from cicd_redteam_agent.application.evaluate import (
    MatrixCaseResult,
    _build_evaluation_payload,
    _render_evaluation_markdown,
)


def _case(variant: str, *, passed: bool) -> MatrixCaseResult:
    return MatrixCaseResult(
        scenario="gha-01",
        variant=variant,
        passed=passed,
        report_path="analysis.json",
        deterministic_duration_seconds=0.25,
        reasoning_duration_seconds=1.5,
        evaluation={
            "extraction_complete": True,
            "forbidden_absent": True,
            "status_correct": passed,
            "missing_evidence_preserved": passed,
        },
    )


def test_evaluation_payload_aggregates_accuracy_and_timing() -> None:
    payload = _build_evaluation_payload(
        [_case("supported", passed=True), _case("rejected", passed=False)]
    )
    metrics = payload["metrics"]

    assert payload["passed_cases"] == 1
    assert metrics["fact_extraction_complete"] == 2
    assert metrics["status_correct"] == 1
    assert metrics["safe_variants_without_false_positive"] == 0
    assert metrics["deterministic_total_seconds"] == 0.5
    assert metrics["reasoning_total_seconds"] == 3.0


def test_evaluation_markdown_contains_prototype_scorecard() -> None:
    payload = _build_evaluation_payload(
        [_case("supported", passed=True), _case("rejected", passed=True)]
    )
    markdown = _render_evaluation_markdown(payload)

    assert "| Fact 추출 성공 | 2/2 |" in markdown
    assert "| 안전 variant 오탐 없음 | 1/1 |" in markdown
    assert "| LLM 미사용 분석 시간 | 0.500초 |" in markdown
    assert "| LLM 사용 분석 시간 | 3.000초 |" in markdown
