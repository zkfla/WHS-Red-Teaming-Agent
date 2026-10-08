"""Small durable SQLite WorkStore.

This is intentionally a ledger, not another domain database.  Rows contain
work lifecycle, opaque graph/evidence references and leases only.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    lease_owner_matches,
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


class SQLiteWorkStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._setup_lock = asyncio.Lock()
        self._ready = False

    async def setup(self) -> None:
        if self._ready:
            return
        async with self._setup_lock:
            if self._ready:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            aiosqlite = _aiosqlite()
            async with aiosqlite.connect(self._path) as db:
                await db.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE IF NOT EXISTS work_items (
                        work_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL DEFAULT '',
                        instance_id TEXT NOT NULL DEFAULT '',
                        candidate_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority REAL NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_work_status_priority
                        ON work_items(status, priority DESC, work_id);
                    CREATE TABLE IF NOT EXISTS work_leases (
                        work_id TEXT PRIMARY KEY REFERENCES work_items(work_id) ON DELETE CASCADE,
                        owner TEXT NOT NULL,
                        issued_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    """
                )
                cursor = await db.execute("PRAGMA table_info(work_items)")
                columns = {row[1] for row in await cursor.fetchall()}
                await cursor.close()
                if "run_id" not in columns:
                    await db.execute(
                        "ALTER TABLE work_items ADD COLUMN run_id TEXT NOT NULL DEFAULT ''"
                    )
                if "instance_id" not in columns:
                    await db.execute(
                        "ALTER TABLE work_items ADD COLUMN instance_id TEXT NOT NULL DEFAULT ''"
                    )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_work_run_status_priority "
                    "ON work_items(run_id,status,priority DESC,work_id)"
                )
                await db.execute("DROP INDEX IF EXISTS idx_one_active_candidate")
                await db.execute("DROP INDEX IF EXISTS idx_one_active_candidate_run")
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_candidate_instance "
                    "ON work_items(run_id,instance_id,candidate_key) "
                    "WHERE status IN ('pending','leased','running')"
                )
                await db.commit()
            self._ready = True

    async def add(self, item: WorkItem) -> bool:
        await self.setup()
        aiosqlite = _aiosqlite()
        try:
            async with aiosqlite.connect(self._path) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT 1 FROM work_items WHERE work_id=?", (item.work_id,)
                )
                collision = await cursor.fetchone()
                await cursor.close()
                if collision is not None:
                    await db.rollback()
                    return False
                cursor = await db.execute(
                    "SELECT payload FROM work_items "
                    "WHERE run_id=? AND instance_id=? AND candidate_key=?",
                    (_run_key(item.run_id), _instance_key(item), item.candidate_key),
                )
                same_candidate = [_item_from_payload(row[0]) for row in await cursor.fetchall()]
                await cursor.close()
                verdict, target = decide_add(item, same_candidate)
                if verdict == "reject":
                    await db.rollback()
                    return False
                if target is not None:
                    await _save_item(db, refresh_pending_item(target, item))
                    await db.commit()
                    return False
                await db.execute(
                    "INSERT INTO work_items"
                    "(work_id,run_id,instance_id,candidate_key,status,priority,payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    _item_row(item),
                )
                await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get(self, work_id: str) -> WorkItem | None:
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute("SELECT payload FROM work_items WHERE work_id=?", (work_id,))
            row = await cursor.fetchone()
            await cursor.close()
        return _item_from_payload(row[0]) if row else None

    async def invalidate_pending(self, *, run_id, path_id, step_ids, now) -> list[str]:
        await self.setup()
        changed = []
        async with _aiosqlite().connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT payload FROM work_items WHERE run_id=? AND status='pending'",
                (_run_key(run_id),),
            )
            for row in await cursor.fetchall():
                item = _item_from_payload(row[0])
                if should_invalidate(item, run_id, path_id, step_ids):
                    await _save_item(db, stale_item(item, now, "path_step_changed"))
                    changed.append(item.work_id)
            await cursor.close()
            await db.commit()
        return changed

    async def bind_job(self, work_id: str, *, owner: str, job_id: str) -> bool:
        await self.setup()
        async with _aiosqlite().connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT owner, expires_at FROM work_leases WHERE work_id=?",
                (work_id,),
            )
            lease = await cursor.fetchone()
            await cursor.close()
            item = await _load_item(db, work_id)
            if lease is None:
                await db.rollback()
                return False
            parsed_expiry = datetime.fromisoformat(lease[1])
            candidate = TaskLease(
                work_id=work_id,
                owner=lease[0],
                issued_at=parsed_expiry,
                heartbeat_at=parsed_expiry,
                expires_at=parsed_expiry,
            )
            if not can_bind(
                candidate, item, owner=owner, job_id=job_id, now=datetime.now(timezone.utc)
            ):
                await db.rollback()
                return False
            await _save_item(db, cast(WorkItem, item).model_copy(update={"job_id": job_id}))
            await db.commit()
        return True

    async def list_items(
        self,
        *,
        status: WorkStatus | None = None,
        run_id: RunId | None = None,
    ) -> list[WorkItem]:
        await self.setup()
        aiosqlite = _aiosqlite()
        query = "SELECT payload FROM work_items"
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status.value)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(_run_key(run_id))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority DESC, work_id"
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()
        return [_item_from_payload(row[0]) for row in rows]

    async def acquire(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None:
        validate_lease_ttl(ttl)
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                item = await _load_item(db, work_id)
                if (
                    item is None
                    or item.status is not WorkStatus.PENDING
                    or not lease_owner_matches(item, owner)
                ):
                    await db.rollback()
                    return None
                failed_dependency = await _terminal_dependency_failure(db, item)
                if failed_dependency is not None:
                    dependency_id, dependency_status = failed_dependency
                    dependency_id, dependency_status = failed_dependency
                    await _save_item(
                        db, blocked_for_dependency(item, dependency_id, dependency_status, now)
                    )
                    await db.commit()
                    return None
                if not await _dependencies_succeeded(db, item):
                    await db.rollback()
                    return None
                if await _has_conflict(db, item):
                    await db.rollback()
                    return None
                lease = new_lease(work_id, owner, now, ttl)
                updated = leased_item(item, now)
                await _save_item(db, updated)
                await db.execute(
                    "INSERT INTO work_leases VALUES(?,?,?,?,?)",
                    (
                        work_id,
                        owner,
                        now.isoformat(),
                        now.isoformat(),
                        (now + ttl).isoformat(),
                    ),
                )
                await db.commit()
                return lease
            except Exception:
                await db.rollback()
                raise

    async def heartbeat(
        self,
        work_id: str,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> TaskLease | None:
        validate_lease_ttl(ttl)
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT issued_at,expires_at FROM work_leases WHERE work_id=? AND owner=?",
                (work_id, owner),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or is_expired(datetime.fromisoformat(row[1]), now):
                await db.rollback()
                return None
            lease = renewed_lease(
                TaskLease(
                    work_id=work_id,
                    owner=owner,
                    issued_at=datetime.fromisoformat(row[0]),
                    heartbeat_at=datetime.fromisoformat(row[0]),
                    expires_at=datetime.fromisoformat(row[1]),
                ),
                now,
                ttl,
            )
            item = await _load_item(db, work_id)
            if item is None:
                await db.rollback()
                return None
            await _save_item(db, running_item(item, now))
            await db.execute(
                "UPDATE work_leases SET heartbeat_at=?,expires_at=? WHERE work_id=?",
                (now.isoformat(), (now + ttl).isoformat(), work_id),
            )
            await db.commit()
            return lease

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
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT expires_at FROM work_leases WHERE work_id=? AND owner=?",
                (work_id, owner),
            )
            lease_row = await cursor.fetchone()
            await cursor.close()
            item = await _load_item(db, work_id)
            if lease_row is None or item is None:
                await db.rollback()
                return False
            if is_expired(datetime.fromisoformat(lease_row[0]), now):
                await _save_item(db, stale_item(item, now, "lease_expired"))
                await db.execute("DELETE FROM work_leases WHERE work_id=?", (work_id,))
                await db.commit()
                return False
            await _save_item(db, finished_item(item, status, now, reason))
            await db.execute("DELETE FROM work_leases WHERE work_id=?", (work_id,))
            await db.commit()
            return True

    async def release(self, work_id: str, *, owner: str, now: datetime) -> bool:
        """Atomically return an unexecuted leased item to PENDING."""
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT expires_at FROM work_leases WHERE work_id=? AND owner=?",
                (work_id, owner),
            )
            row = await cursor.fetchone()
            await cursor.close()
            item = await _load_item(db, work_id)
            if row is None or item is None or is_expired(datetime.fromisoformat(row[0]), now):
                await db.rollback()
                return False
            await _save_item(db, pending_item(item, now))
            await db.execute("DELETE FROM work_leases WHERE work_id=?", (work_id,))
            await db.commit()
            return True

    async def reclaim_expired(self, *, now: datetime) -> list[str]:
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT work_id,expires_at FROM work_leases ORDER BY work_id",
            )
            leases = {}
            for row in await cursor.fetchall():
                try:
                    leases[row[0]] = datetime.fromisoformat(row[1])
                except (ValueError, TypeError):
                    continue
            await cursor.close()
            expired = expired_lease_ids(leases, now)
            for work_id in expired:
                item = await _load_item(db, work_id)
                if item is not None:
                    await _save_item(db, stale_item(item, now, "lease_expired"))
            if expired:
                placeholders = ",".join("?" for _ in expired)
                await db.execute(
                    f"DELETE FROM work_leases WHERE work_id IN ({placeholders})",  # noqa: S608
                    tuple(expired),
                )
            await db.commit()
            return expired

    async def abandon_run(
        self,
        run_id: RunId,
        *,
        owner: str,
        now: datetime,
        reason: str,
    ) -> list[str]:
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT payload FROM work_items WHERE run_id=? "
                    "AND status IN ('pending','leased','running')",
                    (_run_key(run_id),),
                )
                items = [_item_from_payload(row[0]) for row in await cursor.fetchall()]
                await cursor.close()
                abandoned: list[str] = []
                for item in items:
                    lease_cursor = await db.execute(
                        "SELECT owner FROM work_leases WHERE work_id=?",
                        (item.work_id,),
                    )
                    lease_row = await lease_cursor.fetchone()
                    await lease_cursor.close()
                    if not abandon_candidate(
                        item, run_id, owner, lease_row[0] if lease_row else None
                    ):
                        continue
                    await _save_item(db, failed_item(item, now, reason))
                    await db.execute(
                        "DELETE FROM work_leases WHERE work_id=?",
                        (item.work_id,),
                    )
                    abandoned.append(item.work_id)
                await db.commit()
                return sorted(abandoned)
            except Exception:
                await db.rollback()
                raise

    async def reconcile_job(self, work_id: str, *, job_id: str, status: str, now: datetime) -> bool:
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                item = await _load_item(db, work_id)
                applied, decided = decide_reconcile(item, job_id, status, now)
                if not applied:
                    await db.rollback()
                    return False
                if decided is not None:
                    await _save_item(db, decided)
                await db.execute("DELETE FROM work_leases WHERE work_id=?", (work_id,))
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise


def _aiosqlite():
    try:
        import aiosqlite
    except ImportError as exc:  # pragma: no cover - optional-extra failure path
        raise RuntimeError('SQLiteWorkStore requires pip install -e ".[orchestration]"') from exc
    return aiosqlite


def _run_key(run_id: RunId | None) -> str:
    return str(run_id) if run_id is not None else ""


def _instance_key(item: WorkItem) -> str:
    return str(item.instance_id) if item.instance_id is not None else ""


def _item_row(item: WorkItem) -> tuple[str, str, str, str, str, float, str]:
    return (
        item.work_id,
        _run_key(item.run_id),
        _instance_key(item),
        item.candidate_key,
        item.status.value,
        item.priority.total,
        item.model_dump_json(),
    )


def _item_from_payload(payload: str) -> WorkItem:
    return WorkItem.model_validate(json.loads(payload))


async def _load_item(db, work_id: str) -> WorkItem | None:
    cursor = await db.execute("SELECT payload FROM work_items WHERE work_id=?", (work_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return _item_from_payload(row[0]) if row else None


async def _save_item(db, item: WorkItem) -> None:
    await db.execute(
        "UPDATE work_items SET status=?,priority=?,payload=? WHERE work_id=?",
        (item.status.value, item.priority.total, item.model_dump_json(), item.work_id),
    )


async def _dependencies_succeeded(db, item: WorkItem) -> bool:
    states: dict[str, WorkStatus | None] = {}
    for dependency in item.prerequisite_ids:
        depended = await _load_item(db, dependency)
        states[dependency] = depended.status if depended is not None else None
    return prereqs_succeeded(states)


async def _terminal_dependency_failure(db, item: WorkItem) -> tuple[str, WorkStatus] | None:
    states: dict[str, WorkStatus] = {}
    for dependency_id in item.prerequisite_ids:
        dependency = await _load_item(db, dependency_id)
        if dependency is not None:
            states[dependency_id] = dependency.status
    return terminal_failure_in(states)


async def _has_conflict(db, item: WorkItem) -> bool:
    cursor = await db.execute(
        "SELECT payload FROM work_items WHERE status IN ('leased','running') AND work_id<>?",
        (item.work_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return has_conflict(item, (_item_from_payload(row[0]) for row in rows))
