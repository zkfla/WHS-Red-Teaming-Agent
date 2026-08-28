import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import CapabilityPolicyViolation
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    ApprovalClaims,
    Capability,
    QueueStatus,
    RiskLevel,
)
from whs_redteam.orchestration.approvals import ApprovalVerifier, sign_approval
from whs_redteam.orchestration.queue import ActionQueue

SECRET = b"0123456789abcdef0123456789abcdef"


def setup(tmp_path: Path) -> tuple[InventoryStore, ActionQueue, ApprovalVerifier]:
    config = EngagementConfig(
        name="approval-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        enabled_capabilities=frozenset({"discovery", "idor_safe"}),
    )
    store = InventoryStore(config.inventory_db_path)
    return store, ActionQueue(store), ApprovalVerifier(config, store, SECRET)


def proposal(path: str = "/orders/1") -> ActionProposal:
    return ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url=f"https://example.test{path}",
        method="GET",
        risk_level=RiskLevel.SAFE_ACTIVE,
        reason="non-owner comparison",
    )


def claims(**updates: object) -> ApprovalClaims:
    now = datetime.now(UTC)
    values = {
        "approval_id": "approval-123",
        "issuer": "security-lead",
        "engagement_name": "approval-test",
        "capabilities": frozenset({Capability.IDOR_SAFE}),
        "targets": ("https://example.test/orders",),
        "methods": frozenset({"GET"}),
        "issued_at": now - timedelta(minutes=1),
        "expires_at": now + timedelta(minutes=10),
        "max_uses": 1,
    }
    values.update(updates)
    return ApprovalClaims.model_validate(values)


def test_valid_approval_atomically_releases_waiting_action(tmp_path: Path) -> None:
    store, queue, verifier = setup(tmp_path)
    token = sign_approval(claims(), SECRET)
    try:
        queued, _ = queue.enqueue("run-1", proposal())
        approved = queue.approve_waiting(queued.queue_id, token, verifier)
        grant = store.connection.execute("SELECT * FROM approval_grants").fetchone()
        database = (tmp_path / "inventory.db").read_bytes()
    finally:
        store.close()

    assert queued.status is QueueStatus.WAITING_APPROVAL
    assert approved.status is QueueStatus.PENDING
    assert approved.approval_verified is True
    assert approved.approval_digest == hashlib.sha256(token.encode()).hexdigest()
    assert approved.proposal.approval_id == "approval-123"
    assert grant is not None
    assert grant["token_digest"] == hashlib.sha256(token.encode()).hexdigest()
    assert grant["use_count"] == 1
    assert token.encode() not in database


def test_tampered_signature_is_rejected_without_queue_change(tmp_path: Path) -> None:
    store, queue, verifier = setup(tmp_path)
    token = sign_approval(claims(), SECRET)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    try:
        queued, _ = queue.enqueue("run-2", proposal())
        with pytest.raises(CapabilityPolicyViolation, match="signature|canonical"):
            queue.approve_waiting(queued.queue_id, tampered, verifier)
        unchanged = queue.get(queued.queue_id)
        grants = store.connection.execute(
            "SELECT COUNT(*) AS count FROM approval_grants"
        ).fetchone()
    finally:
        store.close()

    assert unchanged is not None
    assert unchanged.status is QueueStatus.WAITING_APPROVAL
    assert grants["count"] == 0


def test_expired_approval_is_rejected(tmp_path: Path) -> None:
    store, queue, verifier = setup(tmp_path)
    now = datetime.now(UTC)
    token = sign_approval(
        claims(
            issued_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(minutes=1),
        ),
        SECRET,
    )
    try:
        queued, _ = queue.enqueue("run-3", proposal())
        with pytest.raises(CapabilityPolicyViolation, match="expired"):
            queue.approve_waiting(queued.queue_id, token, verifier)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("claim_updates", "message"),
    [
        ({"engagement_name": "other"}, "different engagement"),
        ({"targets": ("https://example.test/admin",)}, "target"),
        ({"capabilities": frozenset({Capability.XSS_SAFE})}, "capability"),
        ({"methods": frozenset({"POST"})}, "method"),
    ],
)
def test_approval_is_bound_to_requested_action(
    tmp_path: Path, claim_updates: dict[str, object], message: str
) -> None:
    store, queue, verifier = setup(tmp_path)
    token = sign_approval(claims(**claim_updates), SECRET)
    try:
        queued, _ = queue.enqueue("run-4", proposal())
        with pytest.raises(CapabilityPolicyViolation, match=message):
            queue.approve_waiting(queued.queue_id, token, verifier)
    finally:
        store.close()


def test_approval_max_uses_is_enforced_across_actions(tmp_path: Path) -> None:
    store, queue, verifier = setup(tmp_path)
    token = sign_approval(claims(max_uses=1), SECRET)
    try:
        first, _ = queue.enqueue("run-5", proposal("/orders/1"))
        second, _ = queue.enqueue("run-5", proposal("/orders/2"))
        queue.approve_waiting(first.queue_id, token, verifier)
        with pytest.raises(CapabilityPolicyViolation, match="exhausted"):
            queue.approve_waiting(second.queue_id, token, verifier)
        second_after = queue.get(second.queue_id)
    finally:
        store.close()

    assert second_after is not None
    assert second_after.status is QueueStatus.WAITING_APPROVAL


def test_revoked_approval_cannot_release_another_action(tmp_path: Path) -> None:
    store, queue, verifier = setup(tmp_path)
    token = sign_approval(claims(max_uses=2), SECRET)
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        first, _ = queue.enqueue("run-6", proposal("/orders/1"))
        second, _ = queue.enqueue("run-6", proposal("/orders/2"))
        queue.approve_waiting(first.queue_id, token, verifier)
        verifier.revoke(digest)
        with pytest.raises(CapabilityPolicyViolation, match="revoked"):
            queue.approve_waiting(second.queue_id, token, verifier)
    finally:
        store.close()


def test_approval_timestamps_require_timezone() -> None:
    now = datetime.now().replace(microsecond=0)
    with pytest.raises(ValueError, match="timezone"):
        claims(issued_at=now, expires_at=now + timedelta(minutes=10))
