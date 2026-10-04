"""Approval workflow. Owned by Control Team.

Backed by ``ApprovalStore`` (default: ``InMemoryApprovalStore``, so
existing callers that build ``ApprovalService()`` with no arguments
keep working unchanged). Pass
``redteam.control.stores.filesystem.FilesystemApprovalStore`` for a
durable sink -- one JSON file per Approval, overwritten in place on
each state change (unlike Audit's append-only log: an Approval is a
single record that transitions PENDING -> APPROVED/REJECTED/EXPIRED,
not a stream of immutable facts).

TODO(control-team): human-in-the-loop notification channel beyond the
CLI (``redteam.app.cli`` ``approvals list``/``approvals resolve``) --
e.g. Slack/webhook. The CLI recovers run and engagement context from
the durable policy-decision Audit entry when recording a resolution.
The Agent Team's replan loop still ends a run instead of pausing for
pending approval.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Protocol

from redteam.core.contracts.policy import Approval
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ActionId, ApprovalId, new_id


class ApprovalStore(Protocol):
    """Durable sink for ``Approval`` records. Unlike ``AuditStore``, this
    is a mutable-record store (save overwrites), not append-only.
    """

    async def save(self, approval: Approval) -> None: ...

    async def get(self, approval_id: ApprovalId | str) -> Approval | None: ...

    async def list_pending(self) -> list[Approval]: ...


class InMemoryApprovalStore:
    """Default backend -- process-local, lost on restart."""

    def __init__(self) -> None:
        self._approvals: dict[ApprovalId, Approval] = {}

    async def save(self, approval: Approval) -> None:
        self._approvals[approval.approval_id] = approval

    async def get(self, approval_id: ApprovalId | str) -> Approval | None:
        return self._approvals.get(ApprovalId(str(approval_id)))

    async def list_pending(self) -> list[Approval]:
        return [a for a in self._approvals.values() if a.status == ApprovalStatus.PENDING]


class ApprovalService:
    def __init__(self, store: ApprovalStore | None = None) -> None:
        self._store = store or InMemoryApprovalStore()

    async def request(
        self, action_id: ActionId, *, execution_fingerprint: str | None = None
    ) -> Approval:
        approval = Approval(
            approval_id=ApprovalId(new_id("appr")),
            action_id=action_id,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc),
            execution_fingerprint=execution_fingerprint,
        )
        await self._store.save(approval)
        return approval

    async def resolve(
        self, approval_id: ApprovalId | str, status: ApprovalStatus, resolved_by: str
    ) -> Approval:
        normalized_id = ApprovalId(str(approval_id))
        approval = await self._store.get(normalized_id)
        if approval is None:
            raise KeyError(f"no such approval: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError(f"approval is already {approval.status.value}: {approval_id}")
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("resolve status must be approved or rejected")
        updated = approval.model_copy(
            update={
                "status": status,
                "resolved_at": datetime.now(timezone.utc),
                "resolved_by": resolved_by,
            }
        )
        await self._store.save(updated)
        return updated

    async def get(self, approval_id: ApprovalId | str) -> Approval | None:
        return await self._store.get(ApprovalId(str(approval_id)))

    async def list_pending(self) -> list[Approval]:
        return await self._store.list_pending()

    async def sweep_expired(self, ttl_seconds: float) -> list[Approval]:
        """Mark PENDING approvals older than ``ttl_seconds`` as EXPIRED.

        Run ``redteam approvals expire --ttl-seconds N`` periodically
        to apply the configured operational retention window.
        Returns the approvals that were just expired.
        """
        if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("approval TTL must be finite and non-negative")
        now = datetime.now(timezone.utc)
        expired: list[Approval] = []
        for approval in await self._store.list_pending():
            age = (now - approval.requested_at).total_seconds()
            if age >= ttl_seconds:
                updated = approval.model_copy(
                    update={"status": ApprovalStatus.EXPIRED, "resolved_at": now}
                )
                await self._store.save(updated)
                expired.append(updated)
        return expired
