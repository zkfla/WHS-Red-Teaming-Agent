"""Work ledger port owned by Agent Team."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from redteam.core.ids import RunId
from redteam.orchestration.work.models import TaskLease, WorkItem, WorkStatus


class WorkStore(Protocol):
    async def add(self, item: WorkItem) -> bool: ...

    async def get(self, work_id: str) -> WorkItem | None: ...

    async def list_items(
        self,
        *,
        status: WorkStatus | None = None,
        run_id: RunId | None = None,
    ) -> list[WorkItem]: ...

    async def acquire(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None: ...

    async def heartbeat(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None: ...

    async def complete(
        self,
        work_id: str,
        *,
        owner: str,
        status: WorkStatus,
        now: datetime,
        reason: str | None = None,
    ) -> bool: ...

    async def reclaim_expired(self, *, now: datetime) -> list[str]: ...

    async def abandon_run(
        self,
        run_id: RunId,
        *,
        owner: str,
        now: datetime,
        reason: str,
    ) -> list[str]: ...
