"""Small durable SQLite WorkStore.

This is intentionally a ledger, not another domain database.  Rows contain
work lifecycle, opaque graph/evidence references and leases only.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from redteam.core.ids import RunId
from redteam.orchestration.work.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    TaskLease,
    WorkItem,
    WorkStatus,
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
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_work_run_status_priority "
                    "ON work_items(run_id,status,priority DESC,work_id)"
                )
                await db.execute("DROP INDEX IF EXISTS idx_one_active_candidate")
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_candidate_run "
                    "ON work_items(run_id,candidate_key) "
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
                    "SELECT status FROM work_items WHERE run_id=? AND candidate_key=?",
                    (_run_key(item.run_id), item.candidate_key),
                )
                existing = [row[0] for row in await cursor.fetchall()]
                await cursor.close()
                active_values = {status.value for status in ACTIVE_STATUSES}
                if any(status in active_values for status in existing):
                    await db.rollback()
                    return False
                if existing and not item.repeatable:
                    await db.rollback()
                    return False
                await db.execute(
                    "INSERT INTO work_items(work_id,run_id,candidate_key,status,priority,payload) "
                    "VALUES(?,?,?,?,?,?)",
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
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                item = await _load_item(db, work_id)
                if item is None or item.status is not WorkStatus.PENDING:
                    await db.rollback()
                    return None
                failed_dependency = await _terminal_dependency_failure(db, item)
                if failed_dependency is not None:
                    dependency_id, dependency_status = failed_dependency
                    await _save_item(
                        db,
                        item.model_copy(
                            update={
                                "status": WorkStatus.BLOCKED,
                                "updated_at": now,
                                "outcome_reason": (
                                    f"prerequisite_not_succeeded:{dependency_id}:"
                                    f"{dependency_status.value}"
                                ),
                            }
                        ),
                    )
                    await db.commit()
                    return None
                if not await _dependencies_succeeded(db, item):
                    await db.rollback()
                    return None
                if await _has_conflict(db, item):
                    await db.rollback()
                    return None
                lease = TaskLease(
                    work_id=work_id,
                    owner=owner,
                    issued_at=now,
                    heartbeat_at=now,
                    expires_at=now + ttl,
                )
                updated = item.model_copy(
                    update={
                        "status": WorkStatus.LEASED,
                        "attempt": item.attempt + 1,
                        "updated_at": now,
                    }
                )
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
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
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
            if row is None or datetime.fromisoformat(row[1]) <= now:
                await db.rollback()
                return None
            lease = TaskLease(
                work_id=work_id,
                owner=owner,
                issued_at=datetime.fromisoformat(row[0]),
                heartbeat_at=now,
                expires_at=now + ttl,
            )
            item = await _load_item(db, work_id)
            if item is None:
                await db.rollback()
                return None
            await _save_item(
                db,
                item.model_copy(update={"status": WorkStatus.RUNNING, "updated_at": now}),
            )
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
        if status not in TERMINAL_STATUSES:
            raise ValueError("complete requires a terminal status")
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
            if datetime.fromisoformat(lease_row[0]) <= now:
                await _save_item(
                    db,
                    item.model_copy(
                        update={
                            "status": WorkStatus.STALE,
                            "updated_at": now,
                            "outcome_reason": "lease_expired",
                        }
                    ),
                )
                await db.execute("DELETE FROM work_leases WHERE work_id=?", (work_id,))
                await db.commit()
                return False
            await _save_item(
                db,
                item.model_copy(
                    update={
                        "status": status,
                        "updated_at": now,
                        "outcome_reason": reason,
                    }
                ),
            )
            await db.execute("DELETE FROM work_leases WHERE work_id=?", (work_id,))
            await db.commit()
            return True

    async def reclaim_expired(self, *, now: datetime) -> list[str]:
        await self.setup()
        aiosqlite = _aiosqlite()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT work_id FROM work_leases WHERE expires_at<=? ORDER BY work_id",
                (now.isoformat(),),
            )
            expired = [row[0] for row in await cursor.fetchall()]
            await cursor.close()
            for work_id in expired:
                item = await _load_item(db, work_id)
                if item is not None:
                    await _save_item(
                        db,
                        item.model_copy(
                            update={
                                "status": WorkStatus.STALE,
                                "updated_at": now,
                                "outcome_reason": "lease_expired",
                            }
                        ),
                    )
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
                    if lease_row is not None and lease_row[0] != owner:
                        continue
                    await _save_item(
                        db,
                        item.model_copy(
                            update={
                                "status": WorkStatus.FAILED,
                                "updated_at": now,
                                "outcome_reason": reason,
                            }
                        ),
                    )
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


def _aiosqlite():
    try:
        import aiosqlite
    except ImportError as exc:  # pragma: no cover - optional-extra failure path
        raise RuntimeError('SQLiteWorkStore requires pip install -e ".[orchestration]"') from exc
    return aiosqlite


def _run_key(run_id: RunId | None) -> str:
    return str(run_id) if run_id is not None else ""


def _item_row(item: WorkItem) -> tuple[str, str, str, str, float, str]:
    return (
        item.work_id,
        _run_key(item.run_id),
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
    for dependency in item.prerequisite_ids:
        depended = await _load_item(db, dependency)
        if depended is None or depended.status is not WorkStatus.SUCCEEDED:
            return False
    return True


async def _terminal_dependency_failure(
    db, item: WorkItem
) -> tuple[str, WorkStatus] | None:
    for dependency_id in item.prerequisite_ids:
        dependency = await _load_item(db, dependency_id)
        if (
            dependency is not None
            and dependency.status in TERMINAL_STATUSES
            and dependency.status is not WorkStatus.SUCCEEDED
        ):
            return dependency_id, dependency.status
    return None


async def _has_conflict(db, item: WorkItem) -> bool:
    keys = set(item.conflict_keys)
    if not keys:
        return False
    cursor = await db.execute(
        "SELECT payload FROM work_items "
        "WHERE status IN ('leased','running') AND work_id<>?",
        (item.work_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return any(not keys.isdisjoint(_item_from_payload(row[0]).conflict_keys) for row in rows)
