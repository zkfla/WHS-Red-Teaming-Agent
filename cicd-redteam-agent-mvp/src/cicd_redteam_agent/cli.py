from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import AttackerProfile, ReportStatus
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundaryError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cicd-agent")
    subcommands = parser.add_subparsers(dest="command", required=True)
    analyze = subcommands.add_parser("analyze", help="statically analyze one local repository")
    analyze.add_argument("repo_path")
    analyze.add_argument("--output", required=True, type=Path)
    analyze.add_argument("--model", help="LiteLLM model identifier; omit for deterministic mode")
    analyze.add_argument(
        "--attacker-profile",
        choices=[item.value for item in AttackerProfile],
        default=AttackerProfile.FORK_PR.value,
    )
    analyze.add_argument("--max-rounds", type=int, default=12)
    analyze.add_argument("--max-tokens", type=int, default=100_000)
    analyze.add_argument("--max-cost", type=float, default=2.0)
    analyze.add_argument("--max-duration", type=int, default=900)
    return parser


def _validate_positive(args: argparse.Namespace) -> None:
    values = {
        "max_rounds": args.max_rounds,
        "max_tokens": args.max_tokens,
        "max_cost": args.max_cost,
        "max_duration": args.max_duration,
    }
    invalid = [name for name, value in values.items() if value <= 0]
    if invalid:
        raise ValueError(f"Limits must be positive: {', '.join(invalid)}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_positive(args)
        config = AnalysisConfig(
            output_dir=args.output,
            attacker_profile=AttackerProfile(args.attacker_profile),
            model=args.model,
            max_rounds=args.max_rounds,
            max_tokens=args.max_tokens,
            max_cost=args.max_cost,
            max_duration=args.max_duration,
        )
        report, run_dir = asyncio.run(analyze_repository(args.repo_path, config))
    except (RepositoryBoundaryError, ValueError, ValidationError) as exc:
        print(f"Input/configuration error: {exc}", file=sys.stderr)
        return 2
    except (ImportError, ModuleNotFoundError) as exc:
        print(f"Framework initialization error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        if args.model:
            print(
                f"Framework or model initialization error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 3
        print(f"Internal validation error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    print(f"{report.status}: {run_dir}")
    if report.status is ReportStatus.COMPLETE:
        return 0
    if report.status is ReportStatus.PARTIAL:
        return 4
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
