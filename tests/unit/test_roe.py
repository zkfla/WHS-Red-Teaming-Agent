"""ROE stage tests (control-team, track-a).

Uses the PM-confirmed naming convention (docs/contracts/actions.md):
``capability`` is a namespace (recon/web/mobile/analysis); operation
policy is matched against the canonical action key
``<capability>.<operation>``. No prefix/wildcard matching.
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
    ok, reasons = check_roe(_proposal(), _engagement())
    assert ok is True
    assert reasons == []


def test_action_not_touched_by_rules_allows() -> None:
    roe = [RuleOfEngagement(name="no-uploads", denied_operations=["web.file_upload"])]
    ok, reasons = check_roe(_proposal(capability="recon", operation="port_scan"), _engagement(roe))
    assert ok is True
    assert reasons == []


def test_denied_operation_matches_canonical_action_key() -> None:
    # denied_operations holds the "<capability>.<operation>" key.
    roe = [RuleOfEngagement(name="no-portscan", denied_operations=["recon.port_scan"])]
    ok, reasons = check_roe(_proposal(capability="recon", operation="port_scan"), _engagement(roe))
    assert ok is False
    assert "recon.port_scan" in reasons[0]
    assert "no-portscan" in reasons[0]


def test_bare_operation_name_does_not_match() -> None:
    # "port_scan" (no capability prefix) must NOT match — the key is
    # "recon.port_scan". Guards against regressing to bare-operation match.
    roe = [RuleOfEngagement(name="wrong", denied_operations=["port_scan"])]
    ok, _ = check_roe(_proposal(capability="recon", operation="port_scan"), _engagement(roe))
    assert ok is True


def test_denied_capability_is_denied() -> None:
    roe = [RuleOfEngagement(name="no-mobile", denied_capabilities=["mobile"])]
    proposal = _proposal(capability="mobile", operation="app_launch")
    ok, reasons = check_roe(proposal, _engagement(roe))
    assert ok is False
    assert "mobile" in reasons[0]


def test_capability_allow_list_is_restrictive() -> None:
    roe = [RuleOfEngagement(name="recon-only", allowed_capabilities=["recon"])]
    assert check_roe(_proposal(capability="recon"), _engagement(roe))[0] is True
    ok, reasons = check_roe(_proposal(capability="web", operation="http_request"), _engagement(roe))
    assert ok is False
    assert "allowed-capability" in reasons[0]


def test_operation_allow_list_matches_canonical_key() -> None:
    roe = [
        RuleOfEngagement(
            name="ops",
            allowed_operations=["recon.port_scan", "recon.service_discovery"],
        )
    ]
    assert check_roe(_proposal(operation="service_discovery"), _engagement(roe))[0] is True
    ok, reasons = check_roe(_proposal(operation="file_upload"), _engagement(roe))
    assert ok is False
    assert "recon.file_upload" in reasons[0]


def test_deny_wins_over_allow_list() -> None:
    roe = [
        RuleOfEngagement(
            name="mixed",
            allowed_operations=["recon.port_scan"],
            denied_operations=["recon.port_scan"],
        )
    ]
    ok, reasons = check_roe(_proposal(operation="port_scan"), _engagement(roe))
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
