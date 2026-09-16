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
e.g. Slack/webhook. Also: resolving a pending Approval from a separate
CLI invocation than the one that saw the original REQUIRE_APPROVAL
updates the Approval record correctly, but does not (and currently
cannot) produce an APPROVAL_RESOLVED audit entry -- that still requires
going through ``DefaultControlPlane.resolve_approval`` in the *same*
process that holds the ``ActionProposal`` in
``_pending_proposals``. Closing that gap needs either persisting
run_id/engagement_id on Approval itself (a core contract change) or a
lookup path from action_id back to run_id/engagement_id (e.g. via
Audit's own POLICY_DECISION entry for that action) -- left as a
follow-up rather than done here, since it's moot until Agent Team's
replan loop can actually pause a run for a pending approval in the
first place (today ``replan_node`` ends the run regardless).
"""

from __future__ import annotations

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

    async def get(self, approval_id: ApprovalId) -> Approval | None: ...

    async def list_pending(self) -> list[Approval]: ...


class InMemoryApprovalStore:
    """Default backend -- process-local, lost on restart."""

    def __init__(self) -> None:
        self._approvals: dict[ApprovalId, Approval] = {}

    async def save(self, approval: Approval) -> None:
        self._approvals[approval.approval_id] = approval

    async def get(self, approval_id: ApprovalId) -> Approval | None:
        return self._approvals.get(approval_id)

    async def list_pending(self) -> list[Approval]:
        return [a for a in self._approvals.values() if a.status == ApprovalStatus.PENDING]


class ApprovalService:
    def __init__(self, store: ApprovalStore | None = None) -> None:
        self._store = store or InMemoryApprovalStore()

    async def request(self, action_id: ActionId) -> Approval:
        approval = Approval(
            approval_id=ApprovalId(new_id("appr")),
            action_id=action_id,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc),
        )
        await self._store.save(approval)
        return approval

    async def resolve(
        self, approval_id: ApprovalId, status: ApprovalStatus, resolved_by: str
    ) -> Approval:
        approval = await self._store.get(approval_id)
        if approval is None:
            raise KeyError(f"no such approval: {approval_id}")
        updated = approval.model_copy(
            update={
                "status": status,
                "resolved_at": datetime.now(timezone.utc),
                "resolved_by": resolved_by,
            }
        )
        await self._store.save(updated)
        return updated

    async def get(self, approval_id: ApprovalId) -> Approval | None:
        return await self._store.get(approval_id)

    async def list_pending(self) -> list[Approval]:
        return await self._store.list_pending()

    async def sweep_expired(self, ttl_seconds: float) -> list[Approval]:
        """Mark PENDING approvals older than ``ttl_seconds`` as EXPIRED.

        Not called automatically anywhere yet -- run it periodically
        (a cron-style job, or a CLI command) or before relying on
        ``list_pending()`` if stale approvals matter for the caller.
        Returns the approvals that were just expired.
        """
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
