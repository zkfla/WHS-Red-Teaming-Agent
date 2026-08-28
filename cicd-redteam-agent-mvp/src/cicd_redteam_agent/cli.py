from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from cicd_redteam_agent.application.analyze import (
    AnalysisConfig,
    ResumeLimitOverrides,
    analyze_repository,
    resume_analysis,
)
from cicd_redteam_agent.application.objective import (
    load_analysis_objective,
    normalize_objective,
)
from cicd_redteam_agent.domain.models import AnalysisMode, AttackerProfile, ReportStatus
from cicd_redteam_agent.framework.auth import ModelProvider
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundaryError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cicd-agent")
    subcommands = parser.add_subparsers(dest="command", required=True)
    analyze = subcommands.add_parser("analyze", help="statically analyze one local repository")
    analyze.add_argument("repo_path")
    analyze.add_argument("--output", required=True, type=Path)
    analyze.add_argument(
        "--model",
        help="LiteLLM or Codex model identifier; omit for deterministic/Codex-default mode",
    )
    analyze.add_argument(
        "--provider",
        choices=[item.value for item in ModelProvider],
        default=ModelProvider.AUTO.value,
        help="credential/backend selection; use codex-chatgpt for saved Codex login",
    )
    analyze.add_argument(
        "--mode",
        choices=[item.value for item in AnalysisMode],
        default=None,
        help=(
            "analysis mode; defaults to reasoning when a model/Codex provider is configured, "
            "otherwise deterministic"
        ),
    )
    analyze.add_argument(
        "--env-file",
        type=Path,
        help="trusted operator .env path; the analyzed repository is never used",
    )
    analyze.add_argument(
        "--attacker-profile",
        choices=[item.value for item in AttackerProfile],
        default=AttackerProfile.FORK_PR.value,
    )
    analyze.add_argument("--max-rounds", type=int, default=12)
    analyze.add_argument("--max-tokens", type=int, default=100_000)
    analyze.add_argument("--max-cost", type=float, default=2.0)
    analyze.add_argument("--max-duration", type=int, default=900)
    objective_group = analyze.add_mutually_exclusive_group()
    objective_group.add_argument(
        "--objective",
        help="trusted natural-language objective for reasoning analysis",
    )
    objective_group.add_argument(
        "--objective-file",
        help="read trusted analysis objective from UTF-8 file; use '-' for stdin",
    )
    analyze.add_argument(
        "--no-objective-prompt",
        action="store_true",
        help="do not prompt for an analysis objective in an interactive reasoning session",
    )
    analyze.add_argument(
        "--no-mode-prompt",
        action="store_true",
        help="keep the non-interactive default instead of prompting for an analysis mode",
    )
    resume = subcommands.add_parser(
        "resume", help="resume a reasoning analysis from its last safe checkpoint"
    )
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--env-file", type=Path)
    resume.add_argument("--max-rounds", type=int)
    resume.add_argument("--max-tokens", type=int)
    resume.add_argument("--max-cost", type=float)
    resume.add_argument("--max-duration", type=int)
    evaluate = subcommands.add_parser(
        "evaluate", help="run reasoning mode against a hidden-oracle fixture matrix"
    )
    evaluate.add_argument("matrix_path", type=Path)
    evaluate.add_argument("--output", required=True, type=Path)
    evaluate.add_argument("--model")
    evaluate.add_argument(
        "--provider",
        choices=[item.value for item in ModelProvider],
        default=ModelProvider.AUTO.value,
    )
    evaluate.add_argument("--env-file", type=Path)
    evaluate.add_argument(
        "--attacker-profile",
        choices=[item.value for item in AttackerProfile],
        default=AttackerProfile.FORK_PR.value,
    )
    evaluate.add_argument("--max-rounds", type=int, default=12)
    evaluate.add_argument("--max-tokens", type=int, default=100_000)
    evaluate.add_argument("--max-cost", type=float, default=2.0)
    evaluate.add_argument("--max-duration", type=int, default=900)
    evaluate.add_argument("--scenario", action="append", default=[])
    evaluate.add_argument(
        "--variant",
        action="append",
        choices=["supported", "candidate", "rejected"],
        default=[],
    )
    return parser


def _validate_positive(args: argparse.Namespace) -> None:
    if args.command == "resume":
        values = {
            "max_rounds": args.max_rounds,
            "max_tokens": args.max_tokens,
            "max_cost": args.max_cost,
            "max_duration": args.max_duration,
        }
        invalid = [
            name for name, value in values.items() if value is not None and value <= 0
        ]
        if invalid:
            raise ValueError(f"Limits must be positive: {', '.join(invalid)}")
        return
    values = {
        "max_rounds": args.max_rounds,
        "max_tokens": args.max_tokens,
        "max_cost": args.max_cost,
        "max_duration": args.max_duration,
    }
    invalid = [name for name, value in values.items() if value <= 0]
    if invalid:
        raise ValueError(f"Limits must be positive: {', '.join(invalid)}")
    if (
        not args.model
        and args.provider not in {ModelProvider.AUTO.value, ModelProvider.CODEX_CHATGPT.value}
    ):
        raise ValueError("--model is required when selecting a LiteLLM provider")
    if args.env_file and args.provider == ModelProvider.CODEX_CHATGPT.value:
        raise ValueError("--env-file is not used with --provider codex-chatgpt")


def _requested_analysis_mode(args: argparse.Namespace) -> AnalysisMode:
    if args.mode:
        return AnalysisMode(args.mode)
    if args.model or args.provider == ModelProvider.CODEX_CHATGPT.value:
        return AnalysisMode.REASONING
    return AnalysisMode.DETERMINISTIC


def _prompt_analysis_mode() -> AnalysisMode:
    print("분석 방식을 선택하세요.")
    print("1. 기본 공격 경로 분석")
    print("   Evidence와 실행 흐름을 규칙·그래프로 빠르게 연결합니다.")
    print("2. AI 심층 공격 경로 분석 (추천)")
    print("   기본 분석 결과를 바탕으로 LLM이 복합 경로를 추가 조사합니다.")
    while True:
        print("선택 번호를 입력하세요 (1 또는 2): ", end="", flush=True)
        value = sys.stdin.readline()
        selected = value.strip().casefold()
        if selected in {"1", "deterministic"}:
            return AnalysisMode.DETERMINISTIC
        if selected in {"2", "reasoning"}:
            return AnalysisMode.REASONING
        print("1 또는 2를 입력해주세요.")


def _prompt_analysis_objective() -> str | None:
    print("분석 목표를 입력하세요 (Enter: 전체 범위 분석): ", end="", flush=True)
    value = sys.stdin.readline()
    if not value or not value.strip():
        return None
    return normalize_objective(value)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_positive(args)
        if args.command == "resume":
            report, run_dir = asyncio.run(
                resume_analysis(
                    args.run_dir,
                    env_file=args.env_file,
                    limits=ResumeLimitOverrides(
                        max_rounds=args.max_rounds,
                        max_tokens=args.max_tokens,
                        max_cost=args.max_cost,
                        max_duration=args.max_duration,
                    ),
                )
            )
            print(f"{report.status}: {run_dir}")
            if report.status is ReportStatus.COMPLETE:
                return 0
            if report.status is ReportStatus.PARTIAL:
                return 4
            return 5
        if (
            args.command == "analyze"
            and args.mode is None
            and args.model is None
            and args.provider == ModelProvider.AUTO.value
            and not args.no_mode_prompt
            and sys.stdin.isatty()
        ):
            selected_mode = _prompt_analysis_mode()
            args.mode = selected_mode.value
            if selected_mode is AnalysisMode.REASONING:
                args.provider = ModelProvider.CODEX_CHATGPT.value
                print("Reasoning 모드는 저장된 Codex ChatGPT 로그인을 사용합니다.")
            _validate_positive(args)
        objective = None
        if args.command == "analyze":
            objective = load_analysis_objective(
                objective=args.objective,
                objective_file=args.objective_file,
                repository_root=args.repo_path,
                stdin=sys.stdin,
            )
            if (
                objective is None
                and _requested_analysis_mode(args) is AnalysisMode.REASONING
                and not args.no_objective_prompt
                and sys.stdin.isatty()
            ):
                objective = _prompt_analysis_objective()
        config = AnalysisConfig(
            output_dir=args.output,
            attacker_profile=AttackerProfile(args.attacker_profile),
            model=args.model,
            provider=ModelProvider(args.provider),
            analysis_mode=(
                AnalysisMode(args.mode)
                if args.command == "analyze" and args.mode
                else (AnalysisMode.REASONING if args.command == "evaluate" else None)
            ),
            env_file=args.env_file,
            max_rounds=args.max_rounds,
            max_tokens=args.max_tokens,
            max_cost=args.max_cost,
            max_duration=args.max_duration,
            objective=objective,
        )
        if objective is not None and config.resolved_mode is AnalysisMode.DETERMINISTIC:
            raise ValueError("analysis objective requires reasoning mode")
        if args.command == "evaluate":
            from cicd_redteam_agent.application.evaluate import evaluate_reasoning_matrix

            results, summary_path = asyncio.run(
                evaluate_reasoning_matrix(
                    args.matrix_path,
                    config,
                    scenario_filter=set(args.scenario) or None,
                    variant_filter=set(args.variant) or None,
                )
            )
            passed = sum(item.passed for item in results)
            print(f"Evaluation: {passed}/{len(results)} passed: {summary_path}")
            return 0 if results and passed == len(results) else 4
        report, run_dir = asyncio.run(analyze_repository(args.repo_path, config))
    except (RepositoryBoundaryError, ValueError, ValidationError) as exc:
        print(f"Input/configuration error: {redact(str(exc))}", file=sys.stderr)
        return 2
    except (ImportError, ModuleNotFoundError) as exc:
        print(f"Framework initialization error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except Exception as exc:
        if (
            getattr(args, "model", None)
            or getattr(args, "provider", None) == ModelProvider.CODEX_CHATGPT.value
            or args.command == "resume"
        ):
            print(
                "Framework or model initialization error: "
                f"{type(exc).__name__}: {redact(str(exc))}",
                file=sys.stderr,
            )
            return 3
        print(
            f"Internal validation error: {type(exc).__name__}: {redact(str(exc))}",
            file=sys.stderr,
        )
        return 5
    print(f"{report.status}: {run_dir}")
    if report.status is ReportStatus.COMPLETE:
        return 0
    if report.status is ReportStatus.PARTIAL:
        return 4
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
