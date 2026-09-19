"""Verdict is not ToolResult.success — the axis the skeleton collapsed."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import Verdict
from redteam.core.ids import ActionId, InvocationId, ToolId
from redteam.orchestration.expectations import Expectation, ExpectationOutcome, verdict_for

NOW = datetime.now(timezone.utc)


def result(
    success: bool = True,
    output: dict[str, object] | str | None = None,
    error_code: str = "boom",
) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("recon.ping"),
        success=success,
        output=output,
        error=None if success else ToolError(code=error_code, message="failed"),
        started_at=NOW,
        finished_at=NOW,
    )


def test_a_failed_execution_yields_unknown_not_pass() -> None:
    # The old placeholder mapped "not success" to UNKNOWN but "success"
    # to PASS regardless of what was being tested; only half of that was
    # right.
    verdict, reason = verdict_for(Expectation(kind="tool_succeeded"), result(success=False))
    assert verdict is Verdict.UNKNOWN
    assert "boom" in reason


def test_success_with_nothing_under_test_is_pass() -> None:
    verdict, reason = verdict_for(None, result(output={"echo": {}}))
    assert verdict is Verdict.PASS
    assert "no expectation" in reason


def test_a_met_violation_expectation_is_a_violation() -> None:
    expectation = Expectation(
        kind="output_field_equals",
        field="status",
        value=200,
        violation_if_met=True,
        description="an unauthenticated caller must not get 200 here",
    )
    verdict, reason = verdict_for(expectation, result(output={"status": 200}))
    assert verdict is Verdict.VIOLATION
    assert "unauthenticated" in reason


def test_a_met_benign_expectation_is_a_pass() -> None:
    expectation = Expectation(kind="output_field_present", field="echo")
    assert verdict_for(expectation, result(output={"echo": 1}))[0] is Verdict.PASS


def test_an_unmet_expectation_does_not_confirm_the_hypothesis() -> None:
    expectation = Expectation(
        kind="output_field_equals", field="status", value=200, violation_if_met=True
    )
    verdict, reason = verdict_for(expectation, result(output={"status": 403}))
    assert verdict is Verdict.PASS
    assert "not confirmed" in reason


def test_an_unmet_expectation_can_be_an_anomaly() -> None:
    expectation = Expectation(kind="output_field_present", field="echo", anomaly_if_unmet=True)
    assert verdict_for(expectation, result(output={"other": 1}))[0] is Verdict.ANOMALY


def test_an_unevaluable_expectation_is_unknown_not_a_quiet_pass() -> None:
    expectation = Expectation(kind="output_field_equals", field="status", value=200)
    assert verdict_for(expectation, result(output="plain text"))[0] is Verdict.UNKNOWN
    assert verdict_for(expectation, result(output={"other": 1}))[0] is Verdict.UNKNOWN


def test_output_contains_looks_at_the_whole_payload() -> None:
    expectation = Expectation(kind="output_contains", value="nginx")
    assert expectation.evaluate(result(output="server: nginx/1.25")) is ExpectationOutcome.MET
    assert expectation.evaluate(result(output={"server": "nginx"})) is ExpectationOutcome.MET
    assert expectation.evaluate(result(output="apache")) is ExpectationOutcome.NOT_MET
    assert expectation.evaluate(result(output=None)) is ExpectationOutcome.INDETERMINATE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "output_field_present"},
        {"kind": "output_field_equals", "value": 1},
        {"kind": "output_contains"},
    ],
)
def test_incomplete_expectations_are_rejected_at_construction(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        Expectation(**kwargs)
