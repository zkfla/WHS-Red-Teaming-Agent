"""W5 engine wiring: execution checks read fresh internal material only."""

from __future__ import annotations

from redteam.orchestration.diagnostics import diagnostic_for_exception
from redteam.orchestration.engine import (
    DecisionRuleError,
    _rejection_feedback,
)


def test_rejection_feedback_codes_stay_distinct():
    assert _rejection_feedback("unknown_operation", "").startswith("unknown_operation")
    assert _rejection_feedback("missing_required_argument", "").startswith(
        "invalid_arguments"
    )
    assert _rejection_feedback("unresolved_target", "").startswith("unresolved_target")
    assert _rejection_feedback("target_mismatch", "").startswith("unresolved_target")
    assert _rejection_feedback("tool_outside_capability_namespace", "").startswith(
        "tool_outside_capability_namespace"
    )
    assert "weird-field" not in _rejection_feedback("unknown_operation", "weird-field")


def test_decision_rule_error_carries_code_location():
    error = DecisionRuleError(
        "action operation is not a supported action",
        code="unknown_operation",
        location="$.actions[0].operation",
    )
    assert error.diagnostic.code == "unknown_operation"
    assert error.diagnostic.location == "$.actions[0].operation"
    assert (
        diagnostic_for_exception(error, stage="action_compilation") is error.diagnostic
    )
    legacy = DecisionRuleError("plain summary")
    assert legacy.diagnostic.code == "decision_rule_violation"
