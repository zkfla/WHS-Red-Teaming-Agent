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
from cicd_redteam_agent.application.github_event import load_github_event_locator
from cicd_redteam_agent.application.natural_cli import (
    is_github_repository_url,
    materialize_target,
    parse_natural_request,
)
from cicd_redteam_agent.application.objective import (
    load_analysis_objective,
    normalize_objective,
)
from cicd_redteam_agent.application.report import render_terminal_brief
from cicd_redteam_agent.domain.models import (
    AnalysisMode,
    AnalysisReport,
    AttackerProfile,
    ReportStatus,
)
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
    analyze.add_argument(
        "--observe-github",
        action="store_true",
        help=(
            "opt in to read-only GitHub.com repository-policy observation before model rounds; "
            "requires GH_TOKEN or GITHUB_TOKEN from trusted operator configuration"
        ),
    )
    analyze.add_argument(
        "--observe-aws",
        action="store_true",
        help=(
            "opt in to read-only AWS IAM role trust and OIDC provider observation; "
            "requires --observe-github and AWS credentials"
        ),
    )
    analyze.add_argument(
        "--aws-profile",
        help="optional AWS shared-config profile for --observe-aws",
    )
    analyze.add_argument(
        "--github-run-id",
        type=int,
        help=(
            "optional GitHub Actions workflow run ID for read-only runtime evidence such as "
            "pending environment deployments; requires --observe-github"
        ),
    )
    analyze.add_argument(
        "--github-event-file",
        type=Path,
        help=(
            "trusted GitHub webhook event JSON outside the analyzed repository; normalizes only "
            "event/resource/principal locators and requires --github-run-id --observe-github"
        ),
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
        invalid = [name for name, value in values.items() if value is not None and value <= 0]
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
    if not args.model and args.provider not in {
        ModelProvider.AUTO.value,
        ModelProvider.CODEX_CHATGPT.value,
    }:
        raise ValueError("--model is required when selecting a LiteLLM provider")
    if (
        args.env_file
        and args.provider == ModelProvider.CODEX_CHATGPT.value
        and not getattr(args, "observe_github", False)
    ):
        raise ValueError("--env-file is not used with --provider codex-chatgpt")
    github_run_id = getattr(args, "github_run_id", None)
    if github_run_id is not None:
        if github_run_id <= 0:
            raise ValueError("--github-run-id must be positive")
        if not getattr(args, "observe_github", False):
            raise ValueError("--github-run-id requires --observe-github")
    if getattr(args, "github_event_file", None) is not None:
        if github_run_id is None:
            raise ValueError("--github-event-file requires --github-run-id")
        if not getattr(args, "observe_github", False):
            raise ValueError("--github-event-file requires --observe-github")
    if getattr(args, "observe_aws", False) and not getattr(args, "observe_github", False):
        raise ValueError("--observe-aws requires --observe-github")
    if getattr(args, "aws_profile", None) and not getattr(args, "observe_aws", False):
        raise ValueError("--aws-profile requires --observe-aws")


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


def _result_exit_code(report: AnalysisReport) -> int:
    if report.status is ReportStatus.COMPLETE:
        return 0
    if report.status is ReportStatus.PARTIAL:
        return 4
    return 5


def _print_existing_result(result_file: Path) -> int:
    run_dir = result_file.parent
    report = AnalysisReport.model_validate_json(result_file.read_text(encoding="utf-8"))
    print("기존 분석 결과를 읽어서 설명함.")
    print(render_terminal_brief(report, run_dir))
    return _result_exit_code(report)


def _latest_analysis_result(root: Path) -> Path | None:
    candidates = [
        *root.glob("analysis.json"),
        *root.glob("*/analysis.json"),
        *root.glob("*/*/analysis.json"),
    ]
    files = [path for path in candidates if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime_ns) if files else None


def _wants_latest_result(text: str) -> bool:
    lowered = text.casefold()
    return "마지막" in lowered and "분석" in lowered and any(
        value in lowered for value in ("결과", "리포트", "보고서", "설명", "보여")
    )


def _natural_analysis(initial_request: str | None = None) -> int:
    print("CI/CD Red Team Agent 자연어 모드")
    print("저장소와 원하는 분석을 한 문장으로 적으면 됨.")
    print("예: https://github.com/org/repo 이 저장소의 CI/CD 취약점과 공격 경로를 분석해줘")
    request_text = initial_request
    if request_text is None:
        print("\n분석 요청> ", end="", flush=True)
        request_text = sys.stdin.readline().strip()
    if not request_text:
        raise ValueError("분석 요청이 비어 있습니다.")

    if _wants_latest_result(request_text):
        latest = _latest_analysis_result(Path.cwd())
        if latest is not None:
            return _print_existing_result(latest)

    try:
        request = parse_natural_request(request_text)
    except ValueError as exc:
        if "분석 대상 저장소" not in str(exc):
            raise
        print("분석 대상 저장소의 로컬 경로 또는 GitHub URL> ", end="", flush=True)
        target = sys.stdin.readline().strip()
        if not target:
            raise ValueError("분석 대상 저장소가 필요합니다.") from exc
        request = parse_natural_request(request_text, explicit_target=target)

    cache_root = Path.home() / ".cicd-redteam-agent" / "repositories"
    if is_github_repository_url(request.target):
        print("GitHub 저장소를 분석용 로컬 복제본으로 가져오는 중...")
    repository = materialize_target(request.target, cache_root=cache_root)
    result_file = repository if repository.is_file() else repository / "analysis.json"
    if result_file.is_file() and result_file.name == "analysis.json":
        return _print_existing_result(result_file)

    output_dir = Path.cwd() / "cicd-agent-runs"

    print(
        "분석 설정: "
        f"mode={request.mode.value}, model={request.model or 'codex-default'}, "
        f"attacker={request.attacker_profile.value}, "
        f"github-observation={'on' if request.observe_github else 'off'}, "
        f"aws-observation={'on' if request.observe_aws else 'off'}"
    )
    if request.mode is AnalysisMode.REASONING:
        print("AI 심층 분석은 저장된 Codex ChatGPT 로그인을 사용함.")

    config = AnalysisConfig(
        output_dir=output_dir,
        attacker_profile=request.attacker_profile,
        model=request.model,
        provider=request.provider,
        analysis_mode=request.mode,
        max_rounds=16,
        max_tokens=300_000,
        max_cost=2.0,
        max_duration=1800,
        objective=request.objective,
        observe_github=request.observe_github,
        observe_aws=request.observe_aws,
        github_run_id=request.github_run_id,
    )
    report, run_dir = asyncio.run(analyze_repository(repository, config))
    print(f"{report.status}: {run_dir}")
    print(render_terminal_brief(report, run_dir))
    return _result_exit_code(report)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    known_commands = {"analyze", "resume", "evaluate"}
    natural_request: str | None = None
    if not raw_argv:
        try:
            return _natural_analysis()
        except (RepositoryBoundaryError, ValueError, ValidationError) as exc:
            print(f"Input/configuration error: {redact(str(exc))}", file=sys.stderr)
            return 2
        except (ImportError, ModuleNotFoundError) as exc:
            print(f"Framework initialization error: {redact(str(exc))}", file=sys.stderr)
            return 3
        except Exception as exc:
            print(
                "Framework or model initialization error: "
                f"{type(exc).__name__}: {redact(str(exc))}",
                file=sys.stderr,
            )
            return 3
    elif raw_argv[0] not in known_commands and not raw_argv[0].startswith("-"):
        natural_request = " ".join(raw_argv)
    if natural_request is not None:
        try:
            return _natural_analysis(natural_request)
        except (RepositoryBoundaryError, ValueError, ValidationError) as exc:
            print(f"Input/configuration error: {redact(str(exc))}", file=sys.stderr)
            return 2
        except (ImportError, ModuleNotFoundError) as exc:
            print(f"Framework initialization error: {redact(str(exc))}", file=sys.stderr)
            return 3
        except Exception as exc:
            print(
                "Framework or model initialization error: "
                f"{type(exc).__name__}: {redact(str(exc))}",
                file=sys.stderr,
            )
            return 3

    args = build_parser().parse_args(raw_argv)
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
            print(render_terminal_brief(report, run_dir))
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
        github_event_locator = None
        if args.command == "analyze" and args.github_event_file is not None:
            github_event_locator = load_github_event_locator(
                args.github_event_file,
                repository_root=args.repo_path,
            )
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
            observe_github=(bool(args.observe_github) if args.command == "analyze" else False),
            observe_aws=(bool(args.observe_aws) if args.command == "analyze" else False),
            aws_profile=(args.aws_profile if args.command == "analyze" else None),
            github_run_id=(args.github_run_id if args.command == "analyze" else None),
            github_event_locator=github_event_locator,
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
    print(render_terminal_brief(report, run_dir))
    if report.status is ReportStatus.COMPLETE:
        return 0
    if report.status is ReportStatus.PARTIAL:
        return 4
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
