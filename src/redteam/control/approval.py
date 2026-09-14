"""Approval workflow, in-memory. Owned by Control Team.

TODO(control-team): persistence, human-in-the-loop notification channel
(Slack/CLI/webhook), expiry sweeping.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.policy import Approval
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ActionId, ApprovalId, new_id


class ApprovalService:
    def __init__(self) -> None:
        self._approvals: dict[ApprovalId, Approval] = {}

    async def request(self, action_id: ActionId) -> Approval:
        approval = Approval(
            approval_id=ApprovalId(new_id("appr")),
            action_id=action_id,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc),
        )
        self._approvals[approval.approval_id] = approval
        return approval

    async def resolve(
        self, approval_id: ApprovalId, status: ApprovalStatus, resolved_by: str
    ) -> Approval:
        approval = self._approvals[approval_id]
        updated = approval.model_copy(
            update={
                "status": status,
                "resolved_at": datetime.now(timezone.utc),
                "resolved_by": resolved_by,
            }
        )
        self._approvals[approval_id] = updated
        return updated

    async def get(self, approval_id: ApprovalId) -> Approval | None:
        return self._approvals.get(approval_id)
