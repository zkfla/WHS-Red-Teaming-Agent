from __future__ import annotations

import json
import time
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
    deterministic_duration_seconds: float
    reasoning_duration_seconds: float
    evaluation: dict[str, object]


def _build_evaluation_payload(results: list[MatrixCaseResult]) -> dict[str, object]:
    total = len(results)
    rejected = [item for item in results if item.variant == "rejected"]
    metrics = {
        "fact_extraction_complete": sum(
            bool(item.evaluation["extraction_complete"]) for item in results
        ),
        "status_correct": sum(bool(item.evaluation["status_correct"]) for item in results),
        "missing_evidence_preserved": sum(
            bool(item.evaluation["missing_evidence_preserved"]) for item in results
        ),
        "safe_variants_without_false_positive": sum(
            bool(item.evaluation["forbidden_absent"])
            and bool(item.evaluation["status_correct"])
            for item in rejected
        ),
        "safe_variant_cases": len(rejected),
        "deterministic_total_seconds": sum(
            item.deterministic_duration_seconds for item in results
        ),
        "reasoning_total_seconds": sum(item.reasoning_duration_seconds for item in results),
    }
    return {
        "passed": bool(results) and all(item.passed for item in results),
        "passed_cases": sum(item.passed for item in results),
        "total_cases": total,
        "metrics": metrics,
        "cases": [asdict(item) for item in results],
    }


def _render_evaluation_markdown(payload: dict[str, object]) -> str:
    total = int(payload["total_cases"])
    metrics = dict(payload["metrics"])
    safe_total = int(metrics["safe_variant_cases"])
    rows = [
        ("전체 fixture", str(total)),
        ("전체 평가 통과", f"{payload['passed_cases']}/{total}"),
        ("Fact 추출 성공", f"{metrics['fact_extraction_complete']}/{total}"),
        ("상태 판정 정확", f"{metrics['status_correct']}/{total}"),
        ("missing_evidence 보존", f"{metrics['missing_evidence_preserved']}/{total}"),
        (
            "안전 variant 오탐 없음",
            f"{metrics['safe_variants_without_false_positive']}/{safe_total}",
        ),
        ("LLM 미사용 분석 시간", f"{float(metrics['deterministic_total_seconds']):.3f}초"),
        ("LLM 사용 분석 시간", f"{float(metrics['reasoning_total_seconds']):.3f}초"),
    ]
    lines = [
        "# Reasoning Matrix Evaluation",
        "",
        "| 평가 항목 | 결과 |",
        "|---|---:|",
        *[f"| {name} | {value} |" for name, value in rows],
        "",
        "> LLM 사용 시간은 저장소 파싱, Fact 추출, 모델 추론, 검증을 포함한 reasoning "
        "전체 시간입니다.",
    ]
    return "\n".join(lines).rstrip() + "\n"


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
    deterministic_config = AnalysisConfig(
        output_dir=output_root / "deterministic-runs",
        attacker_profile=config.attacker_profile,
        analysis_mode=AnalysisMode.DETERMINISTIC,
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
            repository = scenario_root / variant
            deterministic_started = time.monotonic()
            await analyze_repository(repository, deterministic_config)
            deterministic_duration = time.monotonic() - deterministic_started
            reasoning_started = time.monotonic()
            report, run_dir = await analyze_repository(
                repository, reasoning_config
            )
            reasoning_duration = time.monotonic() - reasoning_started
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
                    deterministic_duration_seconds=deterministic_duration,
                    reasoning_duration_seconds=reasoning_duration,
                    evaluation=asdict(evaluation),
                )
            )
    summary_path = evaluation_root / "evaluation.json"
    payload = _build_evaluation_payload(results)
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (evaluation_root / "evaluation.md").write_text(
        _render_evaluation_markdown(payload), encoding="utf-8"
    )
    return results, summary_path
