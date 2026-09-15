"""ROE stage seam test.

Locks in the skeleton wiring (control-team): ``check_roe`` is a
pass-through today, and ``DefaultControlPlane`` runs it as a real
pipeline stage between Scope and Policy without changing the allow path.
When track-a fills in real ROE rules, add DENY cases here.
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


def _proposal(host: str = "good.local") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_roe"),
        run_id=RunId("run_roe"),
        engagement_id=EngagementId("eng_roe"),
        agent_id=AgentId("agent_roe"),
        capability="recon.example",
        operation="ping",
        target=HostTarget(host=host),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def test_check_roe_passes_through_without_rules() -> None:
    ok, reasons = check_roe(_proposal(), _engagement())
    assert ok is True
    assert reasons == []


def test_check_roe_passes_through_even_with_rules() -> None:
    # Skeleton behavior: rules present but not yet enforced.
    roe = [RuleOfEngagement(name="no-availability-impact", denied_operations=["ddos"])]
    ok, reasons = check_roe(_proposal(), _engagement(roe))
    assert ok is True
    assert reasons == []


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
