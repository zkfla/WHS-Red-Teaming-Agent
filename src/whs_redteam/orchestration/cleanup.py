"""Persistent LIFO cleanup queue for compensating state-changing test actions."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from whs_redteam.exceptions import QueueTransitionError
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    Capability,
    CleanupStatus,
    CleanupTask,
    ToolBackend,
    utc_now,
)
from whs_redteam.orchestration.adapters import ProbeContext, ToolAdapter
from whs_redteam.sanitizer import sanitize_data


class CleanupQueue:
    def __init__(
        self,
        inventory: InventoryStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.inventory = inventory
        self.clock = clock

    def enqueue(
        self,
        *,
        run_id: str,
        parent_action_id: str,
        parent_queue_id: str | None,
        capability: Capability,
        backend: ToolBackend,
        tool_name: str,
        arguments: dict[str, object],
        max_attempts: int = 2,
    ) -> tuple[CleanupTask, bool]:
        task, inserted = self.reserve(
            run_id=run_id,
            parent_action_id=parent_action_id,
            parent_queue_id=parent_queue_id,
            capability=capability,
            backend=backend,
            tool_name=tool_name,
            max_attempts=max_attempts,
        )
        if task.status is CleanupStatus.RESERVED:
            task = self.arm(task.cleanup_id, arguments)
        return task, inserted

    def reserve(
        self,
        *,
        run_id: str,
        parent_action_id: str,
        parent_queue_id: str | None,
        capability: Capability,
        backend: ToolBackend,
        tool_name: str,
        max_attempts: int = 2,
    ) -> tuple[CleanupTask, bool]:
        if not 1 <= max_attempts <= 3:
            raise ValueError("cleanup max_attempts must be between 1 and 3")
        if not tool_name:
            raise ValueError("cleanup tool name must not be empty")
        now = self.clock()
        cleanup_id = uuid4().hex
        cursor = self.inventory.connection.execute(
            """
            INSERT OR IGNORE INTO cleanup_tasks (
                cleanup_id, run_id, parent_action_id, parent_queue_id,
                capability, backend, tool_name, arguments_json, status,
                attempt_count, max_attempts, worker_id, lease_expires_at, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                cleanup_id,
                run_id,
                parent_action_id,
                parent_queue_id,
                capability.value,
                backend.value,
                tool_name,
                "{}",
                CleanupStatus.RESERVED.value,
                max_attempts,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self.inventory.connection.commit()
        row = self.inventory.connection.execute(
            "SELECT * FROM cleanup_tasks WHERE parent_action_id = ?",
            (parent_action_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("cleanup task disappeared after enqueue")
        return self._model(row), cursor.rowcount == 1

    def arm(
        self, cleanup_id: str, arguments: dict[str, object]
    ) -> CleanupTask:
        task = self.get(cleanup_id)
        if task is None or task.status is not CleanupStatus.RESERVED:
            raise QueueTransitionError("only RESERVED cleanup tasks can be armed")
        self.inventory.connection.execute(
            """
            UPDATE cleanup_tasks SET arguments_json = ?, status = ?,
                updated_at = ? WHERE cleanup_id = ? AND status = ?
            """,
            (
                json.dumps(
                    sanitize_data(arguments), ensure_ascii=False, sort_keys=True
                ),
                CleanupStatus.PENDING.value,
                self.clock().isoformat(),
                cleanup_id,
                CleanupStatus.RESERVED.value,
            ),
        )
        self.inventory.connection.commit()
        updated = self.get(cleanup_id)
        if updated is None:
            raise RuntimeError("cleanup task disappeared after arming")
        return updated

    def claim_next(
        self, run_id: str, worker_id: str, *, lease_seconds: int = 60
    ) -> CleanupTask | None:
        if lease_seconds < 1:
            raise ValueError("cleanup lease_seconds must be positive")
        connection = self.inventory.connection
        now = self.clock()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE cleanup_tasks SET status = ?, worker_id = NULL,
                    lease_expires_at = NULL,
                    last_error = 'LeaseExpired: cleanup attempts exhausted',
                    updated_at = ?
                WHERE run_id = ? AND status = ? AND lease_expires_at < ?
                  AND attempt_count >= max_attempts
                """,
                (
                    CleanupStatus.FAILED.value,
                    now.isoformat(),
                    run_id,
                    CleanupStatus.RUNNING.value,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE cleanup_tasks SET status = ?, worker_id = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND status = ? AND lease_expires_at < ?
                  AND attempt_count < max_attempts
                """,
                (
                    CleanupStatus.PENDING.value,
                    now.isoformat(),
                    run_id,
                    CleanupStatus.RUNNING.value,
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM cleanup_tasks
                WHERE run_id = ? AND status = ?
                ORDER BY created_at DESC, cleanup_id DESC LIMIT 1
                """,
                (run_id, CleanupStatus.PENDING.value),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """
                UPDATE cleanup_tasks SET status = ?, worker_id = ?,
                    lease_expires_at = ?, attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE cleanup_id = ? AND status = ?
                """,
                (
                    CleanupStatus.RUNNING.value,
                    worker_id,
                    lease.isoformat(),
                    now.isoformat(),
                    row["cleanup_id"],
                    CleanupStatus.PENDING.value,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        task = self.get(str(row["cleanup_id"]))
        if task is None:
            raise RuntimeError("claimed cleanup task disappeared")
        return task

    def get(self, cleanup_id: str) -> CleanupTask | None:
        row = self.inventory.connection.execute(
            "SELECT * FROM cleanup_tasks WHERE cleanup_id = ?", (cleanup_id,)
        ).fetchone()
        return self._model(row) if row is not None else None

    def mark_succeeded(self, cleanup_id: str) -> CleanupTask:
        return self._transition(cleanup_id, CleanupStatus.SUCCEEDED)

    def mark_failed(self, cleanup_id: str, error: Exception) -> CleanupTask:
        task = self.get(cleanup_id)
        if task is None or task.status is not CleanupStatus.RUNNING:
            raise QueueTransitionError("only RUNNING cleanup tasks can fail")
        target = (
            CleanupStatus.PENDING
            if task.attempt_count < task.max_attempts
            else CleanupStatus.FAILED
        )
        return self._transition(cleanup_id, target, error)

    def incomplete(self, run_id: str) -> tuple[CleanupTask, ...]:
        rows = self.inventory.connection.execute(
            """
            SELECT * FROM cleanup_tasks
            WHERE run_id = ? AND status <> ? ORDER BY created_at DESC
            """,
            (run_id, CleanupStatus.SUCCEEDED.value),
        ).fetchall()
        return tuple(self._model(row) for row in rows)

    def _transition(
        self,
        cleanup_id: str,
        target: CleanupStatus,
        error: Exception | None = None,
    ) -> CleanupTask:
        task = self.get(cleanup_id)
        if task is None or task.status is not CleanupStatus.RUNNING:
            raise QueueTransitionError("only RUNNING cleanup tasks can transition")
        message = None
        if error is not None:
            message = f"{type(error).__name__}: {str(error).splitlines()[0][:400]}"
        self.inventory.connection.execute(
            """
            UPDATE cleanup_tasks SET status = ?, worker_id = NULL,
                lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE cleanup_id = ?
            """,
            (target.value, message, self.clock().isoformat(), cleanup_id),
        )
        self.inventory.connection.commit()
        updated = self.get(cleanup_id)
        if updated is None:
            raise RuntimeError("cleanup task disappeared after transition")
        return updated

    @staticmethod
    def _model(row) -> CleanupTask:
        data = dict(row)
        data["capability"] = Capability(data["capability"])
        data["backend"] = ToolBackend(data["backend"])
        data["status"] = CleanupStatus(data["status"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        if data["lease_expires_at"]:
            data["lease_expires_at"] = datetime.fromisoformat(
                data["lease_expires_at"]
            )
        return CleanupTask.model_validate(data)


class CleanupExecutor:
    def __init__(
        self,
        queue: CleanupQueue,
        adapter: ToolAdapter,
        context: ProbeContext,
    ) -> None:
        self.queue = queue
        self.adapter = adapter
        self.context = context

    async def execute(self, task: CleanupTask) -> None:
        try:
            await self.adapter.execute_cleanup(task, self.context)
        except Exception as exc:
            self.queue.mark_failed(task.cleanup_id, exc)
            raise
        self.queue.mark_succeeded(task.cleanup_id)
