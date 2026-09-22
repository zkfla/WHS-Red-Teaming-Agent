"""ROE stage tests (control-team, track-a).

Uses the PM-confirmed naming convention (docs/contracts/actions.md):
``capability`` is a namespace (recon/web/mobile/analysis); operation
policy is matched against the canonical action key
``<capability>.<operation>``. No prefix/wildcard matching.

``check_roe`` returns ``(allowed, reasons, severity)`` — see
``redteam.control.severity`` for how each denial maps to a severity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from redteam.control.roe import UnsupportedRoePolicyError, check_roe
from redteam.control.service import DefaultControlPlane
from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


def _engagement(roe: list[RuleOfEngagement] | None = None) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_roe"),
        name="roe",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        rules_of_engagement=roe or [],
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(
    *, capability: str = "recon", operation: str = "port_scan", host: str = "good.local"
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_roe"),
        run_id=RunId("run_roe"),
        engagement_id=EngagementId("eng_roe"),
        agent_id=AgentId("agent_roe"),
        capability=capability,
        operation=operation,
        target=HostTarget(host=host),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


# --- unit: check_roe ---------------------------------------------------


def test_no_rules_allows() -> None:
    ok, reasons, severity = check_roe(_proposal(), _engagement())
    assert ok is True
    assert reasons == []
    assert severity is None


def test_action_not_touched_by_rules_allows() -> None:
    roe = [RuleOfEngagement(name="no-uploads", denied_operations=["web.file_upload"])]
    ok, reasons, _ = check_roe(
        _proposal(capability="recon", operation="port_scan"), _engagement(roe)
    )
    assert ok is True
    assert reasons == []


def test_denied_operation_matches_canonical_action_key() -> None:
    # denied_operations holds the "<capability>.<operation>" key.
    roe = [RuleOfEngagement(name="no-portscan", denied_operations=["recon.port_scan"])]
    ok, reasons, severity = check_roe(
        _proposal(capability="recon", operation="port_scan"), _engagement(roe)
    )
    assert ok is False
    assert "recon.port_scan" in reasons[0]
    assert "no-portscan" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_bare_operation_name_does_not_match() -> None:
    # "port_scan" (no capability prefix) must NOT match — the key is
    # "recon.port_scan". Guards against regressing to bare-operation match.
    roe = [RuleOfEngagement(name="wrong", denied_operations=["port_scan"])]
    ok, _, _ = check_roe(_proposal(capability="recon", operation="port_scan"), _engagement(roe))
    assert ok is True


def test_denied_capability_is_denied() -> None:
    roe = [RuleOfEngagement(name="no-mobile", denied_capabilities=["mobile"])]
    proposal = _proposal(capability="mobile", operation="app_launch")
    ok, reasons, severity = check_roe(proposal, _engagement(roe))
    assert ok is False
    assert "mobile" in reasons[0]
    assert severity is Severity.REPLAN_GOAL


def test_capability_allow_list_is_restrictive() -> None:
    roe = [RuleOfEngagement(name="recon-only", allowed_capabilities=["recon"])]
    assert check_roe(_proposal(capability="recon"), _engagement(roe))[0] is True
    ok, reasons, severity = check_roe(
        _proposal(capability="web", operation="http_request"), _engagement(roe)
    )
    assert ok is False
    assert "allowed-capability" in reasons[0]
    assert severity is Severity.REPLAN_GOAL


def test_operation_allow_list_matches_canonical_key() -> None:
    roe = [
        RuleOfEngagement(
            name="ops",
            allowed_operations=["recon.port_scan", "recon.service_discovery"],
        )
    ]
    assert check_roe(_proposal(operation="service_discovery"), _engagement(roe))[0] is True
    ok, reasons, severity = check_roe(_proposal(operation="file_upload"), _engagement(roe))
    assert ok is False
    assert "recon.file_upload" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_deny_wins_over_allow_list() -> None:
    roe = [
        RuleOfEngagement(
            name="mixed",
            allowed_operations=["recon.port_scan"],
            denied_operations=["recon.port_scan"],
        )
    ]
    ok, reasons, _ = check_roe(_proposal(operation="port_scan"), _engagement(roe))
    assert ok is False
    assert "denied by ROE rule" in reasons[0]


# --- unit: time windows (injected clock) ------------------------------

_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_start_at_in_future_is_denied() -> None:
    roe = [RuleOfEngagement(name="not-started", start_at=_NOON + timedelta(hours=1))]
    ok, reasons, severity = check_roe(_proposal(), _engagement(roe), now=_NOON)
    assert ok is False
    assert "has not started" in reasons[0]
    assert severity is Severity.ABORT_RUN


def test_end_at_in_past_is_denied() -> None:
    roe = [RuleOfEngagement(name="ended", end_at=_NOON - timedelta(hours=1))]
    ok, reasons, severity = check_roe(_proposal(), _engagement(roe), now=_NOON)
    assert ok is False
    assert "has ended" in reasons[0]
    assert severity is Severity.ABORT_RUN


def test_within_start_end_allows() -> None:
    roe = [
        RuleOfEngagement(
            name="active",
            start_at=_NOON - timedelta(hours=1),
            end_at=_NOON + timedelta(hours=1),
        )
    ]
    assert check_roe(_proposal(), _engagement(roe), now=_NOON)[0] is True


def test_time_window_inside_allows() -> None:
    roe = [RuleOfEngagement(name="business-hours", allowed_time_windows=["09:00-18:00"])]
    assert check_roe(_proposal(), _engagement(roe), now=_NOON)[0] is True


def test_time_window_outside_is_denied() -> None:
    roe = [RuleOfEngagement(name="business-hours", allowed_time_windows=["09:00-11:00"])]
    ok, reasons, severity = check_roe(_proposal(), _engagement(roe), now=_NOON)
    assert ok is False
    assert "time" in reasons[0]
    assert severity is Severity.ABORT_RUN


def test_overnight_window() -> None:
    roe = [RuleOfEngagement(name="night", allowed_time_windows=["22:00-04:00"])]
    late = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    assert check_roe(_proposal(), _engagement(roe), now=late)[0] is True
    assert check_roe(_proposal(), _engagement(roe), now=_NOON)[0] is False


def test_malformed_window_denies_when_only_window() -> None:
    roe = [RuleOfEngagement(name="typo", allowed_time_windows=["not-a-time"])]
    ok, _, _ = check_roe(_proposal(), _engagement(roe), now=_NOON)
    assert ok is False


# --- pipeline: DefaultControlPlane ------------------------------------


async def test_control_plane_allows_in_scope_action_through_roe_stage() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_control_plane_denies_out_of_scope_before_roe() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(host="evil.example"),
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.DENY
    assert "scope" in decision.matched_rules


async def test_control_plane_denies_roe_violation() -> None:
    roe = [RuleOfEngagement(name="no-portscan", denied_operations=["recon.port_scan"])]
    engagement = _engagement(roe)
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(capability="recon", operation="port_scan"),
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.DENY
    assert "roe" in decision.matched_rules
    assert any("recon.port_scan" in r for r in decision.reasons)


# --- availability_impact_allowed ---------------------------------------


def test_availability_impacting_action_denied_by_default() -> None:
    roe = [RuleOfEngagement(name="default")]  # availability_impact_allowed defaults False
    proposal = _proposal().model_copy(update={"availability_impact": True})
    ok, reasons, severity = check_roe(proposal, _engagement(roe))
    assert ok is False
    assert "availability_impact_allowed=False" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_availability_impacting_action_allowed_when_rule_opts_in() -> None:
    roe = [RuleOfEngagement(name="ok-with-impact", availability_impact_allowed=True)]
    proposal = _proposal().model_copy(update={"availability_impact": True})
    assert check_roe(proposal, _engagement(roe))[0] is True


def test_non_impacting_action_unaffected_by_availability_rule() -> None:
    roe = [RuleOfEngagement(name="default")]
    assert check_roe(_proposal(), _engagement(roe))[0] is True


# --- prohibited_techniques: explicit unsupported, never silently ignored --


def test_prohibited_techniques_fails_closed_with_a_clear_error() -> None:
    roe = [RuleOfEngagement(name="no-brute-force", prohibited_techniques=["brute_force"])]
    with pytest.raises(UnsupportedRoePolicyError, match="prohibited_techniques"):
        check_roe(_proposal(), _engagement(roe))


async def test_control_plane_aborts_run_on_unsupported_prohibited_techniques() -> None:
    roe = [RuleOfEngagement(name="no-brute-force", prohibited_techniques=["brute_force"])]
    engagement = _engagement(roe)
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.DENY
    assert any("abort_run" in rule for rule in decision.matched_rules)
    assert any("prohibited_techniques" in reason for reason in decision.reasons)


# --- max_parallel_actions: runtime signal, fail-closed without it ------


def test_max_parallel_actions_denies_at_the_limit() -> None:
    roe = [RuleOfEngagement(name="one-at-a-time", max_parallel_actions=1)]
    ok, reasons, severity = check_roe(_proposal(), _engagement(roe), in_flight_actions=1)
    assert ok is False
    assert "max_parallel_actions=1" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_max_parallel_actions_allows_under_the_limit() -> None:
    roe = [RuleOfEngagement(name="two-at-a-time", max_parallel_actions=2)]
    assert check_roe(_proposal(), _engagement(roe), in_flight_actions=1)[0] is True


def test_max_parallel_actions_fails_closed_without_the_runtime_signal() -> None:
    roe = [RuleOfEngagement(name="one-at-a-time", max_parallel_actions=1)]
    ok, reasons, severity = check_roe(_proposal(), _engagement(roe))  # no in_flight_actions
    assert ok is False
    assert "no in-flight-action count was supplied" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


async def test_control_plane_supplies_in_flight_count_from_context_metadata() -> None:
    roe = [RuleOfEngagement(name="one-at-a-time", max_parallel_actions=1)]
    engagement = _engagement(roe)
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(),
        ControlContext(
            engagement_id=engagement.engagement_id, metadata={"in_flight_actions": 0}
        ),
    )
    assert decision.verdict == PolicyVerdict.ALLOW
