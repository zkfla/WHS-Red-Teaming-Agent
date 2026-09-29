"""Concurrency-correct in-memory WorkStore for tests and local runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from redteam.core.ids import RunId
from redteam.orchestration.work.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    TaskLease,
    WorkItem,
    WorkStatus,
)


class InMemoryWorkStore:
    def __init__(self) -> None:
        self._items: dict[str, WorkItem] = {}
        self._leases: dict[str, TaskLease] = {}
        self._lock = asyncio.Lock()

    async def add(self, item: WorkItem) -> bool:
        async with self._lock:
            if item.work_id in self._items:
                return False
            same_candidate = [
                existing
                for existing in self._items.values()
                if existing.run_id == item.run_id
                and existing.candidate_key == item.candidate_key
            ]
            if any(existing.status in ACTIVE_STATUSES for existing in same_candidate):
                return False
            if same_candidate and not item.repeatable:
                return False
            self._items[item.work_id] = item.model_copy(deep=True)
            return True

    async def get(self, work_id: str) -> WorkItem | None:
        async with self._lock:
            item = self._items.get(work_id)
            return item.model_copy(deep=True) if item is not None else None

    async def list_items(
        self,
        *,
        status: WorkStatus | None = None,
        run_id: RunId | None = None,
    ) -> list[WorkItem]:
        async with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in self._items.values()
                if (status is None or item.status is status)
                and (run_id is None or item.run_id == run_id)
            ]
        return sorted(items, key=lambda item: (-item.priority.total, item.work_id))

    async def acquire(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None:
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._lock:
            item = self._items.get(work_id)
            if item is None or item.status is not WorkStatus.PENDING:
                return None
            failed_dependency = self._terminal_dependency_failure(item)
            if failed_dependency is not None:
                dependency_id, dependency_status = failed_dependency
                self._items[work_id] = item.model_copy(
                    update={
                        "status": WorkStatus.BLOCKED,
                        "updated_at": now,
                        "outcome_reason": (
                            f"prerequisite_not_succeeded:{dependency_id}:"
                            f"{dependency_status.value}"
                        ),
                    }
                )
                return None
            if not self._dependencies_succeeded(item):
                return None
            if self._has_conflict(item):
                return None
            lease = TaskLease(
                work_id=work_id,
                owner=owner,
                issued_at=now,
                heartbeat_at=now,
                expires_at=now + ttl,
            )
            self._leases[work_id] = lease
            self._items[work_id] = item.model_copy(
                update={
                    "status": WorkStatus.LEASED,
                    "attempt": item.attempt + 1,
                    "updated_at": now,
                }
            )
            return lease.model_copy(deep=True)

    async def heartbeat(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None:
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._lock:
            lease = self._leases.get(work_id)
            if lease is None or lease.owner != owner or lease.expires_at <= now:
                return None
            renewed = lease.model_copy(update={"heartbeat_at": now, "expires_at": now + ttl})
            self._leases[work_id] = renewed
            item = self._items[work_id]
            self._items[work_id] = item.model_copy(
                update={"status": WorkStatus.RUNNING, "updated_at": now}
            )
            return renewed.model_copy(deep=True)

    async def complete(
        self,
        work_id: str,
        *,
        owner: str,
        status: WorkStatus,
        now: datetime,
        reason: str | None = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError("complete requires a terminal status")
        async with self._lock:
            lease = self._leases.get(work_id)
            item = self._items.get(work_id)
            if lease is None or item is None or lease.owner != owner:
                return False
            if lease.expires_at <= now:
                self._items[work_id] = item.model_copy(
                    update={
                        "status": WorkStatus.STALE,
                        "updated_at": now,
                        "outcome_reason": "lease_expired",
                    }
                )
                del self._leases[work_id]
                return False
            self._items[work_id] = item.model_copy(
                update={"status": status, "updated_at": now, "outcome_reason": reason}
            )
            del self._leases[work_id]
            return True

    async def release(self, work_id: str, *, owner: str, now: datetime) -> bool:
        """Return an unexecuted leased item to the runnable frontier."""
        async with self._lock:
            lease = self._leases.get(work_id)
            item = self._items.get(work_id)
            if lease is None or item is None or lease.owner != owner or lease.expires_at <= now:
                return False
            self._items[work_id] = item.model_copy(
                update={"status": WorkStatus.PENDING, "updated_at": now}
            )
            del self._leases[work_id]
            return True

    async def reclaim_expired(self, *, now: datetime) -> list[str]:
        async with self._lock:
            expired = sorted(
                work_id for work_id, lease in self._leases.items() if lease.expires_at <= now
            )
            for work_id in expired:
                item = self._items[work_id]
                self._items[work_id] = item.model_copy(
                    update={
                        "status": WorkStatus.STALE,
                        "updated_at": now,
                        "outcome_reason": "lease_expired",
                    }
                )
                del self._leases[work_id]
            return expired

    async def abandon_run(
        self,
        run_id: RunId,
        *,
        owner: str,
        now: datetime,
        reason: str,
    ) -> list[str]:
        async with self._lock:
            abandoned: list[str] = []
            for work_id, item in self._items.items():
                if item.run_id != run_id or item.status not in ACTIVE_STATUSES:
                    continue
                lease = self._leases.get(work_id)
                if lease is not None and lease.owner != owner:
                    continue
                self._items[work_id] = item.model_copy(
                    update={
                        "status": WorkStatus.FAILED,
                        "updated_at": now,
                        "outcome_reason": reason,
                    }
                )
                self._leases.pop(work_id, None)
                abandoned.append(work_id)
            return sorted(abandoned)

    def _dependencies_succeeded(self, item: WorkItem) -> bool:
        return all(
            dependency in self._items and self._items[dependency].status is WorkStatus.SUCCEEDED
            for dependency in item.prerequisite_ids
        )

    def _terminal_dependency_failure(
        self, item: WorkItem
    ) -> tuple[str, WorkStatus] | None:
        for dependency_id in item.prerequisite_ids:
            dependency = self._items.get(dependency_id)
            if (
                dependency is not None
                and dependency.status in TERMINAL_STATUSES
                and dependency.status is not WorkStatus.SUCCEEDED
            ):
                return dependency_id, dependency.status
        return None

    def _has_conflict(self, item: WorkItem) -> bool:
        keys = set(item.conflict_keys)
        if not keys:
            return False
        return any(
            other_id != item.work_id
            and other.status in {WorkStatus.LEASED, WorkStatus.RUNNING}
            and not keys.isdisjoint(other.conflict_keys)
            for other_id, other in self._items.items()
        )
