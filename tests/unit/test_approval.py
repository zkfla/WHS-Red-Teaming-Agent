"""Approval Test: requests/resolutions persist across a restart, and
expiry sweeping moves stale PENDING approvals to EXPIRED.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from redteam.control.approval import ApprovalService, InMemoryApprovalStore
from redteam.control.stores.filesystem import FilesystemApprovalStore
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ActionId


async def test_filesystem_approval_store_persists_across_instances(tmp_path: Path) -> None:
    service_a = ApprovalService(FilesystemApprovalStore(tmp_path))
    approval = await service_a.request(ActionId("action_1"))
    assert approval.status == ApprovalStatus.PENDING

    # Simulate a restart: brand-new service/store pointed at the same directory.
    service_b = ApprovalService(FilesystemApprovalStore(tmp_path))
    fetched = await service_b.get(approval.approval_id)
    assert fetched is not None
    assert fetched.action_id == ActionId("action_1")

    resolved = await service_b.resolve(approval.approval_id, ApprovalStatus.APPROVED, "operator")
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.resolved_by == "operator"

    # A third instance sees the resolution too -- not just the request.
    service_c = ApprovalService(FilesystemApprovalStore(tmp_path))
    fetched_again = await service_c.get(approval.approval_id)
    assert fetched_again is not None
    assert fetched_again.status == ApprovalStatus.APPROVED


async def test_list_pending_excludes_resolved() -> None:
    service = ApprovalService()
    still_pending = await service.request(ActionId("action_1"))
    to_resolve = await service.request(ActionId("action_2"))
    await service.resolve(to_resolve.approval_id, ApprovalStatus.REJECTED, "operator")

    pending = await service.list_pending()
    assert [a.approval_id for a in pending] == [still_pending.approval_id]


async def test_sweep_expired_marks_old_pending_approvals() -> None:
    store = InMemoryApprovalStore()
    service = ApprovalService(store)
    approval = await service.request(ActionId("action_1"))

    # Backdate the request past the TTL, the way real clock skew would --
    # done via the store directly (a handle we kept), not a private
    # attribute, since ApprovalService has no public "backdate" API.
    stale = approval.model_copy(
        update={"requested_at": datetime.now(timezone.utc) - timedelta(seconds=120)}
    )
    await store.save(stale)

    expired = await service.sweep_expired(ttl_seconds=60)
    assert [a.approval_id for a in expired] == [approval.approval_id]

    refreshed = await service.get(approval.approval_id)
    assert refreshed is not None
    assert refreshed.status == ApprovalStatus.EXPIRED
