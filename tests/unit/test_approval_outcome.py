"""``DefaultControlPlane.approval_outcome`` — resume-after-approval decisions.

Covers the ``ControlPlane`` Protocol contract Orchestration's resume path
(``AgentLoopRunner.reconcile_once``) relies on:

    ActionProposal -> evaluate() -> REQUIRE_APPROVAL
                    -> (human) resolve_approval()
                    -> approval_outcome() -> final PolicyDecision

Approval never bypasses Control: only rate limit/budget are re-checked
here (Scope/ROE/Policy already ran once, deterministically, against the
same immutable proposal).
"""

from __future__ import annotations

from datetime import datetime, timezone

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
from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


def _engagement(
    *, max_actions: int = 5, max_actions_per_minute: int = 30
) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_appr"),
        name="approval",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=max_actions),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=max_actions_per_minute),
        approval_policy=ApprovalPolicy(require_approval_for=["mobile"]),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(action_id: str = "action_1") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_appr"),
        agent_id=AgentId("agent_1"),
        capability="mobile",
        operation="static_analysis",
        target=HostTarget(host="good.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


async def _require_approval(plane: DefaultControlPlane, proposal: ActionProposal) -> str:
    decision = await plane.evaluate(
        proposal, ControlContext(engagement_id=proposal.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None
    return decision.approval_id


async def test_pending_approval_has_no_outcome_yet() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    approval_id = await _require_approval(plane, _proposal())

    assert await plane.approval_outcome(approval_id) is None


async def test_approved_action_resumes_to_allow() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    proposal = _proposal()
    approval_id = await _require_approval(plane, proposal)

    await plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")
    decision = await plane.approval_outcome(approval_id)

    assert decision is not None
    assert decision.verdict == PolicyVerdict.ALLOW
    assert decision.action_id == proposal.action_id
    assert decision.approval_id == approval_id


async def test_rejected_action_resumes_to_deny_and_never_executes() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    proposal = _proposal()
    approval_id = await _require_approval(plane, proposal)

    await plane.resolve_approval(approval_id, ApprovalStatus.REJECTED, "operator")
    decision = await plane.approval_outcome(approval_id)

    assert decision is not None
    assert decision.verdict == PolicyVerdict.DENY
    assert "rejected" in decision.reasons[0]


async def test_outcome_is_idempotent_and_does_not_double_consume_budget() -> None:
    engagement = _engagement(max_actions=2)
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    proposal = _proposal()
    approval_id = await _require_approval(plane, proposal)
    await plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")

    first = await plane.approval_outcome(approval_id)
    second = await plane.approval_outcome(approval_id)

    assert first is not None and second is not None
    assert first.verdict == PolicyVerdict.ALLOW
    assert first == second
    # budget was max_actions=2; if the second approval_outcome() call had
    # re-consumed budget, only 0 units would remain instead of 1.
    assert first.budget_remaining == 1


async def test_budget_consumed_by_another_action_denies_a_later_approved_one() -> None:
    engagement = _engagement(max_actions=1)
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    proposal = _proposal()
    approval_id = await _require_approval(plane, proposal)

    # A different, immediately-ALLOWed action consumes the only unit of
    # budget while this one is still waiting on a human.
    other = ActionProposal(
        action_id=ActionId("action_other"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_appr"),
        agent_id=AgentId("agent_1"),
        capability="recon",
        operation="port_scan",
        target=HostTarget(host="good.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )
    other_decision = await plane.evaluate(
        other, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert other_decision.verdict == PolicyVerdict.ALLOW

    await plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")
    decision = await plane.approval_outcome(approval_id)

    assert decision is not None
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.reasons[0]


async def test_duplicate_resolve_is_rejected_by_the_approval_service() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    approval_id = await _require_approval(plane, _proposal())
    await plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")

    import pytest

    with pytest.raises(ValueError, match="already"):
        await plane.resolve_approval(approval_id, ApprovalStatus.REJECTED, "operator_two")

    # The first resolution still stands.
    decision = await plane.approval_outcome(approval_id)
    assert decision is not None
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_stale_approval_fails_closed_when_proposal_context_is_gone() -> None:
    """Simulates a process restart: a fresh DefaultControlPlane never held
    this ActionProposal, but the durable ApprovalStore says it was
    approved. Must never guess the proposal back into existence."""
    engagement = _engagement()
    plane_before_restart = DefaultControlPlane({engagement.engagement_id: engagement})
    approval_id = await _require_approval(plane_before_restart, _proposal())
    approvals_store = plane_before_restart._approvals  # noqa: SLF001 - simulate shared durable store
    await plane_before_restart.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")

    plane_after_restart = DefaultControlPlane(
        {engagement.engagement_id: engagement}, approvals=approvals_store
    )
    decision = await plane_after_restart.approval_outcome(approval_id)

    assert decision is not None
    assert decision.verdict == PolicyVerdict.DENY
    assert "stale" in decision.reasons[0]
