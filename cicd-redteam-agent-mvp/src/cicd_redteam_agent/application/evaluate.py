from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import AnalysisMode
from cicd_redteam_agent.domain.reasoning_evaluation import (
    ReasoningExpectation,
    evaluate_reasoning_report,
)


@dataclass(frozen=True)
class MatrixCaseResult:
    scenario: str
    variant: str
    passed: bool
    report_path: str
    evaluation: dict[str, object]


async def evaluate_reasoning_matrix(
    matrix_root: Path,
    config: AnalysisConfig,
    *,
    scenario_filter: set[str] | None = None,
    variant_filter: set[str] | None = None,
) -> tuple[list[MatrixCaseResult], Path]:
    """Run real reasoning analysis, then score results against hidden fixture oracles."""

    matrix_root = matrix_root.resolve()
    output_root = config.output_dir.resolve()
    evaluation_root = output_root / "matrix-evaluation"
    evaluation_root.mkdir(parents=True, exist_ok=True)
    reasoning_config = AnalysisConfig(
        output_dir=output_root / "runs",
        attacker_profile=config.attacker_profile,
        model=config.model,
        provider=config.provider,
        analysis_mode=AnalysisMode.REASONING,
        env_file=config.env_file,
        max_rounds=config.max_rounds,
        max_tokens=config.max_tokens,
        max_cost=config.max_cost,
        max_duration=config.max_duration,
    )
    results: list[MatrixCaseResult] = []
    for scenario_root in sorted(matrix_root.glob("gha-*")):
        if scenario_filter and scenario_root.name not in scenario_filter:
            continue
        variants = [
            item.name
            for item in scenario_root.iterdir()
            if item.is_dir() and item.name in {"supported", "candidate", "rejected"}
        ]
        for variant in sorted(variants):
            if variant_filter and variant not in variant_filter:
                continue
            report, run_dir = await analyze_repository(
                scenario_root / variant, reasoning_config
            )
            oracle = json.loads(
                (scenario_root / "oracle.json").read_text(encoding="utf-8")
            )
            variant_oracle = oracle["variants"][variant]
            expectation = ReasoningExpectation.from_dict(
                {
                    **variant_oracle,
                    "scenario_ids": oracle["scenario_ids"],
                    "variant": variant,
                }
            )
            evaluation = evaluate_reasoning_report(report, expectation)
            results.append(
                MatrixCaseResult(
                    scenario=scenario_root.name,
                    variant=variant,
                    passed=evaluation.passed,
                    report_path=str(run_dir / "analysis.json"),
                    evaluation=asdict(evaluation),
                )
            )
    summary_path = evaluation_root / "evaluation.json"
    payload = {
        "passed": bool(results) and all(item.passed for item in results),
        "passed_cases": sum(item.passed for item in results),
        "total_cases": len(results),
        "cases": [asdict(item) for item in results],
    }
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return results, summary_path
