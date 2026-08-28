from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import QueueTransitionError
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import ActionProposal, Capability, QueueStatus, RiskLevel
from whs_redteam.orchestration.queue import ActionQueue


def make_queue(tmp_path: Path) -> tuple[InventoryStore, ActionQueue]:
    config = EngagementConfig(
        name="queue-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
    )
    store = InventoryStore(config.inventory_db_path)
    return store, ActionQueue(store)


def discovery(target: str, reason: str = "inventory") -> ActionProposal:
    return ActionProposal(
        capability=Capability.DISCOVERY,
        target_url=target,
        method="GET",
        risk_level=RiskLevel.PASSIVE,
        reason=reason,
    )


def test_enqueue_is_idempotent_even_if_reason_changes(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        first, first_created = queue.enqueue(
            "run-1", discovery("https://example.test/a", "first reason")
        )
        second, second_created = queue.enqueue(
            "run-1", discovery("https://example.test/a", "rewritten reason")
        )
    finally:
        store.close()

    assert first_created is True
    assert second_created is False
    assert first.queue_id == second.queue_id
    assert first.idempotency_key == second.idempotency_key


def test_claim_uses_priority_and_only_one_worker_gets_action(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        low, _ = queue.enqueue(
            "run-2", discovery("https://example.test/low"), priority=10
        )
        high, _ = queue.enqueue(
            "run-2", discovery("https://example.test/high"), priority=200
        )
        claimed = queue.claim_next("run-2", "worker-a")
        other = queue.claim_next("run-2", "worker-b")
    finally:
        store.close()

    assert claimed is not None
    assert claimed.queue_id == high.queue_id
    assert claimed.status is QueueStatus.RUNNING
    assert claimed.attempt_count == 1
    assert other is not None
    assert other.queue_id == low.queue_id


def test_failure_retries_only_until_max_attempts(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        queued, _ = queue.enqueue(
            "run-3", discovery("https://example.test/retry"), max_attempts=2
        )
        first = queue.claim_next("run-3", "worker-a")
        assert first is not None
        retry = queue.mark_failed(first.queue_id, TimeoutError("temporary"))
        second = queue.claim_next("run-3", "worker-b")
        assert second is not None
        failed = queue.mark_failed(second.queue_id, TimeoutError("still failing"))
        exhausted = queue.claim_next("run-3", "worker-c")
    finally:
        store.close()

    assert retry.status is QueueStatus.RETRY_PENDING
    assert second.attempt_count == 2
    assert failed.status is QueueStatus.FAILED
    assert exhausted is None
    assert failed.queue_id == queued.queue_id


def test_invalid_transition_is_rejected(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        queued, _ = queue.enqueue("run-4", discovery("https://example.test/a"))
        with pytest.raises(QueueTransitionError, match="invalid queue transition"):
            queue.mark_succeeded(queued.queue_id, "action-never-ran")
    finally:
        store.close()


def test_sensitive_action_waits_for_approval(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    proposal = ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url="https://example.test/orders/1",
        reason="compare non-owner access",
        risk_level=RiskLevel.SAFE_ACTIVE,
    )
    try:
        queued, _ = queue.enqueue("run-5", proposal)
        claimed = queue.claim_next("run-5", "worker-a")
    finally:
        store.close()

    assert queued.status is QueueStatus.WAITING_APPROVAL
    assert claimed is None


def test_fabricated_approval_id_is_stripped_and_still_waits(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    forged = ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url="https://example.test/orders/1",
        reason="planner must not self-approve",
        approval_id="forged-approval",
    )
    try:
        queued, _ = queue.enqueue("run-forged", forged)
    finally:
        store.close()

    assert queued.status is QueueStatus.WAITING_APPROVAL
    assert queued.approval_verified is False
    assert queued.approval_digest is None
    assert queued.proposal.approval_id is None


def test_expired_final_lease_becomes_failed(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        queued, _ = queue.enqueue(
            "run-6", discovery("https://example.test/lease"), max_attempts=1
        )
        claimed = queue.claim_next("run-6", "dead-worker")
        assert claimed is not None
        expired = datetime.now(UTC) - timedelta(seconds=1)
        store.connection.execute(
            "UPDATE action_queue SET lease_expires_at = ? WHERE queue_id = ?",
            (expired.isoformat(), queued.queue_id),
        )
        store.connection.commit()
        assert queue.claim_next("run-6", "recovery-worker") is None
        recovered = queue.get(queued.queue_id)
    finally:
        store.close()

    assert recovered is not None
    assert recovered.status is QueueStatus.FAILED
    assert recovered.last_error == "LeaseExpired: maximum attempts exhausted"


def test_enqueue_rejects_invalid_queue_limits(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        with pytest.raises(ValueError, match="priority"):
            queue.enqueue(
                "run-7", discovery("https://example.test/a"), priority=1001
            )
        with pytest.raises(ValueError, match="max_attempts"):
            queue.enqueue(
                "run-7", discovery("https://example.test/b"), max_attempts=0
            )
    finally:
        store.close()


def test_queue_counts_support_status_observability(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        queue.enqueue("run-8", discovery("https://example.test/a"))
        queue.enqueue(
            "run-8",
            ActionProposal(
                capability=Capability.IDOR_SAFE,
                target_url="https://example.test/orders/1",
                reason="approval status",
            ),
        )
        counts = store.queue_counts("run-8")
    finally:
        store.close()

    assert counts == {"PENDING": 1, "WAITING_APPROVAL": 1}


def test_dependency_must_succeed_before_higher_priority_action_is_claimed(
    tmp_path: Path,
) -> None:
    store, queue = make_queue(tmp_path)
    try:
        prerequisite, _ = queue.enqueue(
            "run-dependency",
            discovery("https://example.test/discover"),
            priority=10,
        )
        dependent, _ = queue.enqueue(
            "run-dependency",
            discovery("https://example.test/validate"),
            priority=500,
            depends_on=(prerequisite.queue_id,),
        )
        first = queue.claim_next("run-dependency", "worker-a")
        assert first is not None
        queue.mark_succeeded(first.queue_id, "action-discovery")
        second = queue.claim_next("run-dependency", "worker-b")
        dependencies = queue.dependencies(dependent.queue_id)
    finally:
        store.close()

    assert first.queue_id == prerequisite.queue_id
    assert second is not None
    assert second.queue_id == dependent.queue_id
    assert dependencies == (prerequisite.queue_id,)


def test_failed_prerequisite_blocks_dependent_action(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        prerequisite, _ = queue.enqueue(
            "run-blocked",
            discovery("https://example.test/discover"),
            max_attempts=1,
        )
        dependent, _ = queue.enqueue(
            "run-blocked",
            discovery("https://example.test/validate"),
            depends_on=(prerequisite.queue_id,),
        )
        claimed = queue.claim_next("run-blocked", "worker-a")
        assert claimed is not None
        queue.mark_terminal_failed(claimed.queue_id, RuntimeError("terminal"))
        assert queue.claim_next("run-blocked", "worker-b") is None
        blocked = queue.get(dependent.queue_id)
    finally:
        store.close()

    assert blocked is not None
    assert blocked.status is QueueStatus.BLOCKED
    assert blocked.last_error == "DependencyFailed: prerequisite did not succeed"


def test_dependency_must_exist_in_same_run(tmp_path: Path) -> None:
    store, queue = make_queue(tmp_path)
    try:
        other, _ = queue.enqueue(
            "other-run", discovery("https://example.test/other")
        )
        with pytest.raises(QueueTransitionError, match="same agent run"):
            queue.enqueue(
                "current-run",
                discovery("https://example.test/current"),
                depends_on=(other.queue_id,),
            )
        with pytest.raises(QueueTransitionError, match="does not exist"):
            queue.enqueue(
                "current-run",
                discovery("https://example.test/missing"),
                depends_on=("missing-queue-id",),
            )
        count = store.connection.execute(
            "SELECT COUNT(*) AS count FROM action_queue WHERE run_id = 'current-run'"
        ).fetchone()
    finally:
        store.close()

    assert count["count"] == 0


def test_failed_prerequisite_blocks_approval_waiting_dependent(
    tmp_path: Path,
) -> None:
    store, queue = make_queue(tmp_path)
    try:
        prerequisite, _ = queue.enqueue(
            "run-waiting-blocked",
            discovery("https://example.test/discover"),
            max_attempts=1,
        )
        waiting, _ = queue.enqueue(
            "run-waiting-blocked",
            ActionProposal(
                capability=Capability.IDOR_SAFE,
                target_url="https://example.test/orders/1",
                reason="must not request approval after prerequisite failure",
            ),
            depends_on=(prerequisite.queue_id,),
        )
        claimed = queue.claim_next("run-waiting-blocked", "worker-a")
        assert claimed is not None
        queue.mark_terminal_failed(claimed.queue_id, RuntimeError("terminal"))
        assert queue.claim_next("run-waiting-blocked", "worker-b") is None
        blocked = queue.get(waiting.queue_id)
    finally:
        store.close()

    assert blocked is not None
    assert blocked.status is QueueStatus.BLOCKED


def test_retry_available_at_survives_until_clock_reaches_backoff(
    tmp_path: Path,
) -> None:
    class DateClock:
        def __init__(self) -> None:
            self.now = datetime(2026, 1, 1, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    clock = DateClock()
    config = EngagementConfig(
        name="backoff-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
    )
    store = InventoryStore(config.inventory_db_path)
    queue = ActionQueue(store, clock=clock)
    try:
        queued, _ = queue.enqueue(
            "run-backoff", discovery("https://example.test/retry")
        )
        claimed = queue.claim_next("run-backoff", "worker-a")
        assert claimed is not None
        retrying = queue.mark_failed(
            claimed.queue_id, TimeoutError("temporary"), delay_seconds=4
        )
        early = queue.claim_next("run-backoff", "worker-b")
        remaining = queue.next_retry_delay("run-backoff")
        clock.advance(4)
        ready = queue.claim_next("run-backoff", "worker-b")
    finally:
        store.close()

    assert retrying.queue_id == queued.queue_id
    assert retrying.status is QueueStatus.RETRY_PENDING
    assert early is None
    assert remaining == 4
    assert ready is not None
    assert ready.attempt_count == 2
