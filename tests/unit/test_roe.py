"""ROE stage tests (control-team, track-a).

Covers the implemented rules — denied/allowed capabilities and
operations — both as a unit (``check_roe``) and through the full
``DefaultControlPlane`` pipeline. Add cases here as the remaining ROE
fields (time windows, availability impact, prohibited techniques) land.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.roe import check_roe
from redteam.control.service import DefaultControlPlane
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
    *, capability: str = "recon.example", operation: str = "ping", host: str = "good.local"
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
    ok, reasons = check_roe(_proposal(), _engagement())
    assert ok is True
    assert reasons == []


def test_action_not_touched_by_rules_allows() -> None:
    roe = [RuleOfEngagement(name="no-ddos", denied_operations=["ddos"])]
    ok, reasons = check_roe(_proposal(operation="ping"), _engagement(roe))
    assert ok is True
    assert reasons == []


def test_denied_operation_is_denied() -> None:
    roe = [RuleOfEngagement(name="no-ddos", denied_operations=["ddos"])]
    ok, reasons = check_roe(_proposal(operation="ddos"), _engagement(roe))
    assert ok is False
    assert "ddos" in reasons[0]
    assert "no-ddos" in reasons[0]


def test_denied_capability_is_denied() -> None:
    roe = [RuleOfEngagement(name="no-destructive", denied_capabilities=["exploit.destructive"])]
    ok, reasons = check_roe(_proposal(capability="exploit.destructive"), _engagement(roe))
    assert ok is False
    assert "exploit.destructive" in reasons[0]


def test_allow_list_is_restrictive_when_configured() -> None:
    roe = [RuleOfEngagement(name="recon-only", allowed_capabilities=["recon.example"])]
    # In the allow-list -> passes.
    ok, _ = check_roe(_proposal(capability="recon.example"), _engagement(roe))
    assert ok is True
    # Not in the allow-list -> denied by default.
    ok, reasons = check_roe(_proposal(capability="exploit.other"), _engagement(roe))
    assert ok is False
    assert "allowed-capability" in reasons[0]


def test_allowed_operation_list_is_restrictive() -> None:
    roe = [RuleOfEngagement(name="ops", allowed_operations=["ping", "scan"])]
    assert check_roe(_proposal(operation="scan"), _engagement(roe))[0] is True
    ok, reasons = check_roe(_proposal(operation="delete"), _engagement(roe))
    assert ok is False
    assert "allowed-operation" in reasons[0]


def test_deny_wins_over_allow_list() -> None:
    roe = [
        RuleOfEngagement(
            name="mixed",
            allowed_operations=["ping"],
            denied_operations=["ping"],
        )
    ]
    # Denied even though it's also in the allow-list.
    ok, reasons = check_roe(_proposal(operation="ping"), _engagement(roe))
    assert ok is False
    assert "denied by ROE rule" in reasons[0]


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
    engagement = _engagement([RuleOfEngagement(name="no-ddos", denied_operations=["ddos"])])
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(operation="ddos"),
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.DENY
    assert "roe" in decision.matched_rules
    assert any("ddos" in r for r in decision.reasons)
