"""SQLite-backed action queue with idempotency, leases, and bounded retries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from whs_redteam.exceptions import QueueTransitionError
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    ActionResult,
    Capability,
    QueuedAction,
    QueueStatus,
    utc_now,
)
from whs_redteam.orchestration.approvals import ApprovalVerifier
from whs_redteam.orchestration.dispatcher import ActionDispatcher
from whs_redteam.orchestration.retry import (
    ErrorDisposition,
    RetryDecision,
    RetryPolicy,
)
from whs_redteam.policy.capability_catalog import CAPABILITY_CATALOG


class ActionQueue:
    def __init__(
        self,
        inventory: InventoryStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.inventory = inventory
        self.clock = clock

    def enqueue(
        self,
        run_id: str,
        proposal: ActionProposal,
        *,
        priority: int = 100,
        max_attempts: int = 2,
        depends_on: tuple[str, ...] = (),
    ) -> tuple[QueuedAction, bool]:
        if not 0 <= priority <= 1000:
            raise ValueError("priority must be between 0 and 1000")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        dependencies = tuple(dict.fromkeys(depends_on))
        if len(dependencies) > 100:
            raise ValueError("an action may have at most 100 dependencies")
        normalized = proposal.model_copy(update={"approval_id": None})
        material = normalized.model_dump(
            mode="json", exclude={"reason", "approval_id"}
        )
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        key = hashlib.sha256(run_id.encode() + b"|" + encoded).hexdigest()
        now = self.clock()
        spec = CAPABILITY_CATALOG[proposal.capability]
        status = (
            QueueStatus.WAITING_APPROVAL
            if spec.approval_required
            else QueueStatus.PENDING
        )
        queue_id = uuid4().hex
        connection = self.inventory.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            for dependency_id in dependencies:
                dependency = connection.execute(
                    "SELECT run_id FROM action_queue WHERE queue_id = ?",
                    (dependency_id,),
                ).fetchone()
                if dependency is None:
                    raise QueueTransitionError(
                        f"dependency does not exist: {dependency_id}"
                    )
                if dependency["run_id"] != run_id:
                    raise QueueTransitionError(
                        "dependencies must belong to the same agent run"
                    )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO action_queue (
                    queue_id, run_id, idempotency_key, proposal_json, status,
                    priority, attempt_count, max_attempts, worker_id,
                    lease_expires_at, action_id, approval_verified, approval_digest,
                    last_error, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, 0, NULL, NULL, ?, ?, ?)
                """,
                (
                    queue_id, run_id, key, normalized.model_dump_json(), status.value,
                    priority, max_attempts, now.isoformat(), now.isoformat(), now.isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                for dependency_id in dependencies:
                    connection.execute(
                        "INSERT INTO action_dependencies VALUES (?, ?, ?)",
                        (queue_id, dependency_id, now.isoformat()),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        row = self.inventory.connection.execute(
            "SELECT * FROM action_queue WHERE run_id = ? AND idempotency_key = ?",
            (run_id, key),
        ).fetchone()
        if row is None:
            raise RuntimeError("queued action disappeared after insert")
        return self._model(row), cursor.rowcount == 1

    def claim_next(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> QueuedAction | None:
        now = self.clock()
        connection = self.inventory.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE action_queue SET status = ?, worker_id = NULL,
                    lease_expires_at = NULL,
                    last_error = 'LeaseExpired: maximum attempts exhausted',
                    updated_at = ?
                WHERE run_id = ? AND status = ? AND lease_expires_at < ?
                  AND attempt_count >= max_attempts
                """,
                (
                    QueueStatus.FAILED.value, now.isoformat(), run_id,
                    QueueStatus.RUNNING.value, now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE action_queue SET status = ?, worker_id = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND status = ? AND lease_expires_at < ?
                  AND attempt_count < max_attempts
                """,
                (
                    QueueStatus.RETRY_PENDING.value, now.isoformat(), run_id,
                    QueueStatus.RUNNING.value, now.isoformat(),
                ),
            )
            terminal_dependencies = (
                QueueStatus.REJECTED.value,
                QueueStatus.FAILED.value,
                QueueStatus.BLOCKED.value,
                QueueStatus.CANCELLED.value,
            )
            connection.execute(
                """
                UPDATE action_queue SET status = ?, worker_id = NULL,
                    lease_expires_at = NULL,
                    last_error = 'DependencyFailed: prerequisite did not succeed',
                    updated_at = ?
                WHERE run_id = ? AND status IN (?, ?, ?) AND EXISTS (
                    SELECT 1 FROM action_dependencies AS dependency
                    JOIN action_queue AS prerequisite
                      ON prerequisite.queue_id = dependency.depends_on_queue_id
                    WHERE dependency.queue_id = action_queue.queue_id
                      AND prerequisite.status IN (?, ?, ?, ?)
                )
                """,
                (
                    QueueStatus.BLOCKED.value, now.isoformat(), run_id,
                    QueueStatus.PENDING.value, QueueStatus.RETRY_PENDING.value,
                    QueueStatus.WAITING_APPROVAL.value,
                    *terminal_dependencies,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM action_queue
                WHERE run_id = ? AND status IN (?, ?) AND available_at <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM action_dependencies AS dependency
                    JOIN action_queue AS prerequisite
                      ON prerequisite.queue_id = dependency.depends_on_queue_id
                    WHERE dependency.queue_id = action_queue.queue_id
                      AND prerequisite.status <> ?
                  )
                ORDER BY priority DESC, created_at, queue_id LIMIT 1
                """,
                (
                    run_id, QueueStatus.PENDING.value,
                    QueueStatus.RETRY_PENDING.value, now.isoformat(),
                    QueueStatus.SUCCEEDED.value,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """
                UPDATE action_queue SET status = ?, worker_id = ?,
                    lease_expires_at = ?, attempt_count = attempt_count + 1,
                    updated_at = ? WHERE queue_id = ?
                """,
                (
                    QueueStatus.RUNNING.value, worker_id, lease.isoformat(),
                    now.isoformat(), row["queue_id"],
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        claimed = self.get(str(row["queue_id"]))
        if claimed is None:
            raise RuntimeError("claimed action disappeared")
        return claimed

    def approve_waiting(
        self,
        queue_id: str,
        token: str,
        verifier: ApprovalVerifier,
    ) -> QueuedAction:
        item = self.get(queue_id)
        if item is None:
            raise QueueTransitionError(f"queued action not found: {queue_id}")
        if item.status is not QueueStatus.WAITING_APPROVAL:
            raise QueueTransitionError("only WAITING_APPROVAL actions can be approved")
        approved, claims, digest = verifier.verify_for_queue(item.proposal, token)
        now = self.clock()
        connection = self.inventory.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            verifier.consume_in_current_transaction(digest, claims, now)
            cursor = connection.execute(
                """
                UPDATE action_queue SET proposal_json = ?, status = ?,
                    approval_verified = 1, approval_digest = ?, updated_at = ?
                WHERE queue_id = ? AND status = ?
                """,
                (
                    approved.model_dump_json(), QueueStatus.PENDING.value,
                    digest, now.isoformat(), queue_id,
                    QueueStatus.WAITING_APPROVAL.value,
                ),
            )
            if cursor.rowcount != 1:
                raise QueueTransitionError("approval queue transition lost its claim")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        updated = self.get(queue_id)
        if updated is None:
            raise RuntimeError("approved action disappeared")
        return updated

    def get(self, queue_id: str) -> QueuedAction | None:
        row = self.inventory.connection.execute(
            "SELECT * FROM action_queue WHERE queue_id = ?", (queue_id,)
        ).fetchone()
        return self._model(row) if row is not None else None

    def dependencies(self, queue_id: str) -> tuple[str, ...]:
        rows = self.inventory.connection.execute(
            """
            SELECT depends_on_queue_id FROM action_dependencies
            WHERE queue_id = ? ORDER BY depends_on_queue_id
            """,
            (queue_id,),
        ).fetchall()
        return tuple(str(row["depends_on_queue_id"]) for row in rows)

    def find_latest(
        self, run_id: str, capability: Capability
    ) -> QueuedAction | None:
        rows = self.inventory.connection.execute(
            """
            SELECT * FROM action_queue WHERE run_id = ?
            ORDER BY created_at DESC, queue_id DESC
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            item = self._model(row)
            if item.proposal.capability is capability:
                return item
        return None

    def mark_succeeded(self, queue_id: str, action_id: str) -> QueuedAction:
        return self._transition(
            queue_id, {QueueStatus.RUNNING}, QueueStatus.SUCCEEDED, action_id=action_id
        )

    def mark_rejected(self, queue_id: str, error: Exception) -> QueuedAction:
        return self._transition(
            queue_id,
            {QueueStatus.RUNNING},
            QueueStatus.REJECTED,
            last_error=self._error(error),
        )

    def mark_failed(
        self,
        queue_id: str,
        error: Exception,
        *,
        delay_seconds: float = 0.0,
    ) -> QueuedAction:
        item = self.get(queue_id)
        if item is None or item.status is not QueueStatus.RUNNING:
            raise QueueTransitionError("only RUNNING actions can fail")
        target = (
            QueueStatus.RETRY_PENDING
            if item.attempt_count < item.max_attempts
            else QueueStatus.FAILED
        )
        return self._transition(
            queue_id,
            {QueueStatus.RUNNING},
            target,
            last_error=self._error(error),
            available_at=self.clock() + timedelta(seconds=max(0.0, delay_seconds)),
        )

    def mark_terminal_failed(self, queue_id: str, error: Exception) -> QueuedAction:
        return self._transition(
            queue_id,
            {QueueStatus.RUNNING},
            QueueStatus.FAILED,
            last_error=self._error(error),
        )

    def _transition(
        self,
        queue_id: str,
        allowed: set[QueueStatus],
        target: QueueStatus,
        *,
        action_id: str | None = None,
        last_error: str | None = None,
        available_at: datetime | None = None,
    ) -> QueuedAction:
        item = self.get(queue_id)
        if item is None:
            raise QueueTransitionError(f"queued action not found: {queue_id}")
        if item.status not in allowed:
            raise QueueTransitionError(
                f"invalid queue transition: {item.status.value} -> {target.value}"
            )
        self.inventory.connection.execute(
            """
            UPDATE action_queue SET status = ?, worker_id = NULL,
                lease_expires_at = NULL, action_id = COALESCE(?, action_id),
                last_error = ?, available_at = COALESCE(?, available_at),
                updated_at = ? WHERE queue_id = ?
            """,
            (
                target.value,
                action_id,
                last_error,
                available_at.isoformat() if available_at else None,
                self.clock().isoformat(),
                queue_id,
            ),
        )
        self.inventory.connection.commit()
        updated = self.get(queue_id)
        if updated is None:
            raise RuntimeError("queued action disappeared after transition")
        return updated

    def next_retry_delay(self, run_id: str) -> float | None:
        row = self.inventory.connection.execute(
            """
            SELECT MIN(available_at) AS available_at FROM action_queue
            WHERE run_id = ? AND status = ?
            """,
            (run_id, QueueStatus.RETRY_PENDING.value),
        ).fetchone()
        if row is None or row["available_at"] is None:
            return None
        available = datetime.fromisoformat(str(row["available_at"]))
        return max(0.0, (available - self.clock()).total_seconds())

    @staticmethod
    def _error(error: Exception) -> str:
        return f"{type(error).__name__}: {str(error).splitlines()[0][:400]}"

    @staticmethod
    def _model(row) -> QueuedAction:
        return QueuedAction.model_validate(dict(row))


class QueueExecutor:
    def __init__(
        self,
        queue: ActionQueue,
        dispatcher: ActionDispatcher,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.queue = queue
        self.dispatcher = dispatcher
        self.retry_policy = retry_policy or RetryPolicy()

    async def execute(self, item: QueuedAction) -> ActionResult:
        if item.status is not QueueStatus.RUNNING:
            raise QueueTransitionError("executor requires a RUNNING queued action")
        try:
            result = await self.dispatcher.dispatch(
                item.proposal,
                run_id=item.run_id,
                queue_id=item.queue_id,
                approval_verified=item.approval_verified,
            )
        except Exception as exc:
            decision = self.retry_policy.classify(
                exc,
                attempt_count=item.attempt_count,
                max_attempts=item.max_attempts,
            )
            spec = CAPABILITY_CATALOG[item.proposal.capability]
            if spec.cleanup_required and decision.disposition is ErrorDisposition.RETRY:
                decision = RetryDecision(
                    ErrorDisposition.TERMINAL,
                    reason=(
                        "cleanup-required actions must be reconciled before any retry"
                    ),
                )
            if decision.disposition is ErrorDisposition.REJECT:
                self.queue.mark_rejected(item.queue_id, exc)
            elif decision.disposition is ErrorDisposition.RETRY:
                self.queue.mark_failed(
                    item.queue_id,
                    exc,
                    delay_seconds=decision.delay_seconds,
                )
            else:
                self.queue.mark_terminal_failed(item.queue_id, exc)
            raise
        self.queue.mark_succeeded(item.queue_id, result.action_id)
        return result
