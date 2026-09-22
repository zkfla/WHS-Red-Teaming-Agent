"""Approval Test: requests/resolutions persist across a restart, and
expiry sweeping moves stale PENDING approvals to EXPIRED.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("first_status", [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
async def test_resolved_approval_cannot_be_processed_again(first_status: ApprovalStatus) -> None:
    service = ApprovalService()
    approval = await service.request(ActionId("action_1"))
    first = await service.resolve(approval.approval_id, first_status, "operator_one")

    with pytest.raises(ValueError, match="already"):
        await service.resolve(approval.approval_id, ApprovalStatus.APPROVED, "operator_two")

    assert await service.get(approval.approval_id) == first


async def test_expired_approval_cannot_be_resolved() -> None:
    store = InMemoryApprovalStore()
    service = ApprovalService(store)
    approval = await service.request(ActionId("action_1"))
    await store.save(
        approval.model_copy(
            update={"requested_at": datetime.now(timezone.utc) - timedelta(seconds=120)}
        )
    )
    await service.sweep_expired(60)

    with pytest.raises(ValueError, match="already expired"):
        await service.resolve(approval.approval_id, ApprovalStatus.APPROVED, "operator")


async def test_resolve_rejects_nonterminal_status() -> None:
    service = ApprovalService()
    approval = await service.request(ActionId("action_1"))

    with pytest.raises(ValueError, match="must be approved or rejected"):
        await service.resolve(approval.approval_id, ApprovalStatus.PENDING, "operator")

    pending = await service.get(approval.approval_id)
    assert pending is not None
    assert pending.status == ApprovalStatus.PENDING


@pytest.mark.parametrize("ttl", [-1, float("nan"), float("inf")])
async def test_sweep_rejects_invalid_ttl(ttl: float) -> None:
    service = ApprovalService()
    approval = await service.request(ActionId("action_1"))
    with pytest.raises(ValueError, match="finite and non-negative"):
        await service.sweep_expired(ttl)
    pending = await service.get(approval.approval_id)
    assert pending is not None
    assert pending.status == ApprovalStatus.PENDING


async def test_cli_expire_updates_persisted_approvals(tmp_path: Path) -> None:
    store = FilesystemApprovalStore(tmp_path)
    service = ApprovalService(store)
    old = await service.request(ActionId("action_old"))
    recent = await service.request(ActionId("action_recent"))
    await store.save(
        old.model_copy(
            update={"requested_at": datetime.now(timezone.utc) - timedelta(seconds=120)}
        )
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "redteam.app.cli", "approvals", "expire",
            "--ttl-seconds", "60", "--dir", str(tmp_path),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Expired 1 approval(s)" in result.stdout
    restarted = ApprovalService(FilesystemApprovalStore(tmp_path))
    expired = await restarted.get(old.approval_id)
    pending = await restarted.get(recent.approval_id)
    assert expired is not None and pending is not None
    assert expired.status == ApprovalStatus.EXPIRED
    assert pending.status == ApprovalStatus.PENDING
