"""Durable pending-proposal linkage on ``DefaultControlPlane``.

Prompt 6's durable resume needs to reconnect a *fresh* process's
``DefaultControlPlane`` to the Approval and ActionProposal that a previous
process left behind:

    evaluate() -> REQUIRE_APPROVAL -> (durable Approval + checkpointed proposal)
                                          |
                              register_pending_proposal()
                                          |
                              approval_outcome() -> ALLOW/DENY

The link is only ever established from the exact checkpointed proposal the
caller supplies -- never reconstructed from the redacted Audit snapshot --
and only when its ``action_id`` matches the Approval's own. A mismatch fails
closed rather than linking a different action.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.control.approval import ApprovalService, InMemoryApprovalStore
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore
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


def _engagement(*, engagement_id: EngagementId | None = None) -> Engagement:
    return Engagement(
        engagement_id=engagement_id or EngagementId("eng_reg"),
        name="registration",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(require_approval_for=["mobile"]),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(
    action_id: str = "action_reg",
    *,
    engagement_id: EngagementId | None = None,
    reason: str = "test",
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RunId("run_reg"),
        engagement_id=engagement_id or EngagementId("eng_reg"),
        agent_id=AgentId("agent_1"),
        capability="mobile",
        operation="static_analysis",
        target=HostTarget(host="good.local"),
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )


async def _require_approval(
    plane: DefaultControlPlane, proposal: ActionProposal
) -> str:
    decision = await plane.evaluate(
        proposal, ControlContext(engagement_id=proposal.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None
    return decision.approval_id


async def test_fresh_process_resumes_only_after_explicit_registration(tmp_path) -> None:
    """The point of the API: a restart sharing the durable ApprovalStore has
    no in-memory link, and must not be able to link one except through the
    exact checkpointed proposal it was handed."""
    engagement = _engagement()
    before = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=ApprovalService(FilesystemApprovalStore(tmp_path / "approvals")),
    )
    proposal = _proposal()
    approval_id = await _require_approval(before, proposal)
    await before.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")

    after = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=ApprovalService(FilesystemApprovalStore(tmp_path / "approvals")),
    )

    await after.register_pending_proposal(approval_id, proposal)
    decision = await after.approval_outcome(approval_id)

    assert decision is not None
    assert decision.verdict == PolicyVerdict.ALLOW
    assert decision.action_id == proposal.action_id
    assert decision.approval_id == approval_id


async def test_registration_rejects_a_different_action_id() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=ApprovalService(InMemoryApprovalStore()),
    )
    approval_id = await _require_approval(plane, _proposal("action_a"))

    with pytest.raises(ValueError, match="refusing to link"):
        await plane.register_pending_proposal(approval_id, _proposal("action_b"))


async def test_registration_is_idempotent_for_the_same_proposal() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=ApprovalService(InMemoryApprovalStore()),
    )
    proposal = _proposal()
    approval_id = await _require_approval(plane, proposal)

    await plane.register_pending_proposal(approval_id, proposal)
    await plane.register_pending_proposal(approval_id, proposal)  # no-op, no raise


async def test_registration_refuses_to_replace_a_linked_proposal() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=ApprovalService(InMemoryApprovalStore()),
    )
    proposal = _proposal()
    approval_id = await _require_approval(plane, proposal)
    await plane.register_pending_proposal(approval_id, proposal)

    conflicting = proposal.model_copy(update={"reason": "a different action body"})
    with pytest.raises(ValueError, match="already linked"):
        await plane.register_pending_proposal(approval_id, conflicting)


async def test_registration_requires_a_known_approval() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})

    with pytest.raises(KeyError, match="no such approval"):
        await plane.register_pending_proposal("appr_missing", _proposal())


async def test_registration_requires_a_known_engagement() -> None:
    engagement = _engagement(engagement_id=EngagementId("eng_known"))
    approvals = ApprovalService(InMemoryApprovalStore())
    plane = DefaultControlPlane({engagement.engagement_id: engagement}, approvals=approvals)

    proposal = _proposal(engagement_id=EngagementId("eng_unknown"))
    approval = await approvals.request(proposal.action_id)

    with pytest.raises(KeyError, match="unregistered engagement"):
        await plane.register_pending_proposal(approval.approval_id, proposal)
