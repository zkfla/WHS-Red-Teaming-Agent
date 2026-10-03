"""Concurrency-correct in-memory WorkStore for tests and local runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import cast

from redteam.core.ids import RunId
from redteam.orchestration.work.models import (
    TaskLease,
    WorkItem,
    WorkStatus,
)
from redteam.orchestration.work.rules import (
    abandon_candidate,
    blocked_for_dependency,
    can_bind,
    decide_add,
    decide_reconcile,
    expired_lease_ids,
    failed_item,
    finished_item,
    has_conflict,
    is_expired,
    lease_owned,
    leased_item,
    new_lease,
    pending_item,
    prereqs_succeeded,
    refresh_pending_item,
    renewed_lease,
    require_terminal,
    running_item,
    should_invalidate,
    stale_item,
    terminal_failure_in,
    validate_lease_ttl,
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
                if existing.run_id == item.run_id and existing.candidate_key == item.candidate_key
            ]
            verdict, target = decide_add(item, same_candidate)
            if verdict == "reject":
                return False
            if target is not None:
                self._items[target.work_id] = refresh_pending_item(target, item)
                return False
            self._items[item.work_id] = item.model_copy(deep=True)
            return True

    async def get(self, work_id: str) -> WorkItem | None:
        async with self._lock:
            item = self._items.get(work_id)
            return item.model_copy(deep=True) if item is not None else None

    async def invalidate_pending(self, *, run_id, path_id, step_ids, now) -> list[str]:
        changed = []
        async with self._lock:
            for identity, item in self._items.items():
                if should_invalidate(item, run_id, path_id, step_ids):
                    self._items[identity] = stale_item(item, now, "path_step_changed")
                    changed.append(identity)
        return changed

    async def bind_job(self, work_id: str, *, owner: str, job_id: str) -> bool:
        async with self._lock:
            lease = self._leases.get(work_id)
            item = self._items.get(work_id)
            if not can_bind(
                lease, item, owner=owner, job_id=job_id, now=datetime.now(timezone.utc)
            ):
                return False
            self._items[work_id] = cast(WorkItem, item).model_copy(
                update={"job_id": job_id}
            )
        return True

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
        validate_lease_ttl(ttl)
        async with self._lock:
            item = self._items.get(work_id)
            if item is None or item.status is not WorkStatus.PENDING:
                return None
            failed_dependency = self._terminal_dependency_failure(item)
            if failed_dependency is not None:
                dependency_id, dependency_status = failed_dependency
                self._items[work_id] = blocked_for_dependency(
                    item, dependency_id, dependency_status, now
                )
                return None
            if not self._dependencies_succeeded(item):
                return None
            if self._has_conflict(item):
                return None
            lease = new_lease(work_id, owner, now, ttl)
            self._leases[work_id] = lease
            self._items[work_id] = leased_item(item, now)
            return lease.model_copy(deep=True)

    async def heartbeat(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None:
        validate_lease_ttl(ttl)
        async with self._lock:
            lease = self._leases.get(work_id)
            if lease is None or not lease_owned(lease, owner) or is_expired(lease.expires_at, now):
                return None
            renewed = renewed_lease(lease, now, ttl)
            self._leases[work_id] = renewed
            item = self._items[work_id]
            self._items[work_id] = running_item(item, now)
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
        require_terminal(status)
        async with self._lock:
            lease = self._leases.get(work_id)
            item = self._items.get(work_id)
            if lease is None or item is None or lease.owner != owner:
                return False
            if is_expired(lease.expires_at, now):
                self._items[work_id] = stale_item(item, now, "lease_expired")
                del self._leases[work_id]
                return False
            self._items[work_id] = finished_item(item, status, now, reason)
            del self._leases[work_id]
            return True

    async def release(self, work_id: str, *, owner: str, now: datetime) -> bool:
        """Return an unexecuted leased item to the runnable frontier."""
        async with self._lock:
            lease = self._leases.get(work_id)
            item = self._items.get(work_id)
            if (
                lease is None
                or item is None
                or lease.owner != owner
                or is_expired(lease.expires_at, now)
            ):
                return False
            self._items[work_id] = pending_item(item, now)
            del self._leases[work_id]
            return True

    async def reclaim_expired(self, *, now: datetime) -> list[str]:
        async with self._lock:
            expired = expired_lease_ids(
                {work_id: lease.expires_at for work_id, lease in self._leases.items()}, now
            )
            for work_id in expired:
                item = self._items[work_id]
                self._items[work_id] = stale_item(item, now, "lease_expired")
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
                lease = self._leases.get(work_id)
                if not abandon_candidate(item, run_id, owner, lease.owner if lease else None):
                    continue
                self._items[work_id] = failed_item(item, now, reason)
                self._leases.pop(work_id, None)
                abandoned.append(work_id)
            return sorted(abandoned)

    async def reconcile_job(self, work_id: str, *, job_id: str, status: str, now: datetime) -> bool:
        async with self._lock:
            applied, decided = decide_reconcile(self._items.get(work_id), job_id, status, now)
            if not applied:
                return False
            if decided is not None:
                self._items[work_id] = decided
            self._leases.pop(work_id, None)
            return True

    def _dependencies_succeeded(self, item: WorkItem) -> bool:
        return prereqs_succeeded(
            {
                dependency: (self._items[dependency].status if dependency in self._items else None)
                for dependency in item.prerequisite_ids
            }
        )

    def _terminal_dependency_failure(self, item: WorkItem) -> tuple[str, WorkStatus] | None:
        return terminal_failure_in(
            {
                dependency_id: self._items[dependency_id].status
                for dependency_id in item.prerequisite_ids
                if dependency_id in self._items
            }
        )

    def _has_conflict(self, item: WorkItem) -> bool:
        return has_conflict(item, self._items.values())
