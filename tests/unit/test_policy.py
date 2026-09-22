"""Policy engine tests (control-team, track-a).

Approval gating matches either a capability namespace or a canonical
action key ``<capability>.<operation>`` — consistent with ROE and
docs/contracts/actions.md. Denials are ROE's job, not the policy engine's.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.policy import evaluate_policy
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


def _proposal(*, capability: str = "recon", operation: str = "port_scan") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_pol"),
        run_id=RunId("run_pol"),
        engagement_id=EngagementId("eng_pol"),
        agent_id=AgentId("agent_pol"),
        capability=capability,
        operation=operation,
        target=HostTarget(host="good.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


# --- unit: evaluate_policy ---------------------------------------------


def test_no_rule_allows() -> None:
    verdict, _, matched = evaluate_policy(_proposal(), ApprovalPolicy())
    assert verdict == PolicyVerdict.ALLOW
    assert matched == []


def test_capability_level_approval() -> None:
    policy = ApprovalPolicy(require_approval_for=["mobile"])
    verdict, _, matched = evaluate_policy(_proposal(capability="mobile"), policy)
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert matched == ["mobile"]


def test_action_key_level_approval() -> None:
    policy = ApprovalPolicy(require_approval_for=["recon.port_scan"])
    verdict, _, matched = evaluate_policy(_proposal(), policy)
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert matched == ["recon.port_scan"]


def test_bare_operation_does_not_match() -> None:
    # Must be the canonical key "recon.port_scan", not bare "port_scan".
    policy = ApprovalPolicy(require_approval_for=["port_scan"])
    verdict, _, _ = evaluate_policy(_proposal(), policy)
    assert verdict == PolicyVerdict.ALLOW


# --- pipeline: approval path through DefaultControlPlane ---------------


def _engagement(approval: ApprovalPolicy) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_pol"),
        name="pol",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=approval,
        created_at=datetime.now(timezone.utc),
    )


async def _evaluate(engagement: Engagement):
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    return await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )


async def test_pipeline_requires_approval_and_creates_approval_id() -> None:
    approval = ApprovalPolicy(require_approval_for=["recon.port_scan"], auto_approve=False)
    decision = await _evaluate(_engagement(approval))
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


async def test_pipeline_auto_approve_becomes_allow() -> None:
    approval = ApprovalPolicy(require_approval_for=["recon.port_scan"], auto_approve=True)
    decision = await _evaluate(_engagement(approval))
    assert decision.verdict == PolicyVerdict.ALLOW
