"""SQLite path storage using fresh snapshots and the memory domain rules."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from redteam.orchestration.paths.memory import InMemoryPathStore
from redteam.orchestration.paths.models import PathPlan, PathVersionConflict
from redteam.orchestration.sqlite_support import connect, run_transaction

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS path_snapshots (
    run_id TEXT NOT NULL,
    path_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, path_id, version)
);
CREATE TABLE IF NOT EXISTS path_progress (
    run_id TEXT NOT NULL,
    path_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    evidence_ids TEXT NOT NULL,
    PRIMARY KEY (run_id, path_id, version, step_id)
);
CREATE TABLE IF NOT EXISTS path_active (
    run_id TEXT PRIMARY KEY,
    path_id TEXT NOT NULL,
    version INTEGER NOT NULL
);
"""


class SQLitePathStore:
    """Durable path snapshots, active pointers and progress in a single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.executescript(_SCHEMA)
            db.commit()
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        return connect(self._path)

    @staticmethod
    def _load_memory(db: sqlite3.Connection) -> InMemoryPathStore:
        """Read a consistent fresh snapshot from the caller's transaction."""
        memory = InMemoryPathStore()
        for run_id, path_id, version, payload in db.execute(
            "SELECT run_id,path_id,version,payload FROM path_snapshots"
        ):
            memory._snapshots[(run_id, path_id, version)] = PathPlan.model_validate_json(payload)
        for run_id, path_id, version, step_id, status, outcome, evidence in db.execute(
            "SELECT run_id,path_id,version,step_id,status,outcome,evidence_ids FROM path_progress"
        ):
            memory._progress[(run_id, path_id, version, step_id)] = {
                "status": status,
                "outcome": outcome,
                "evidence_ids": json.loads(evidence),
            }
        memory._active = {
            run_id: (path_id, version)
            for run_id, path_id, version in db.execute(
                "SELECT run_id,path_id,version FROM path_active"
            )
        }
        return memory

    @staticmethod
    def _save_memory(db: sqlite3.Connection, memory: InMemoryPathStore) -> None:
        """Persist changed rows without creating or committing another transaction.

        Snapshots stay immutable. Only fresh, staged progress and active pointers
        may change; unrelated rows and orchestration tables are never deleted.
        """
        snapshots = {
            (run_id, path_id, version): payload
            for run_id, path_id, version, payload in db.execute(
                "SELECT run_id,path_id,version,payload FROM path_snapshots"
            )
        }
        for key, plan in memory._snapshots.items():
            payload = plan.model_dump_json()
            previous = snapshots.get(key)
            if previous is not None:
                if PathPlan.model_validate_json(previous) != plan:
                    raise PathVersionConflict("stored path snapshots are immutable")
                continue
            db.execute(
                "INSERT INTO path_snapshots "
                "(run_id,path_id,version,parent_version,status,payload) VALUES(?,?,?,?,?,?)",
                (*key, plan.parent_version, plan.status, payload),
            )
        progress = {
            (run_id, path_id, version, step_id): (status, outcome, json.loads(evidence))
            for run_id, path_id, version, step_id, status, outcome, evidence in db.execute(
                "SELECT run_id,path_id,version,step_id,status,outcome,evidence_ids "
                "FROM path_progress"
            )
        }
        for progress_key, record in memory._progress.items():
            current = (record["status"], record["outcome"], record["evidence_ids"])
            if progress.get(progress_key) != current:
                db.execute(
                    "INSERT INTO path_progress "
                    "(run_id,path_id,version,step_id,status,outcome,evidence_ids) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id,path_id,version,step_id) DO UPDATE SET "
                    "status=excluded.status,outcome=excluded.outcome,evidence_ids=excluded.evidence_ids",
                    (
                        *progress_key,
                        record["status"],
                        record["outcome"],
                        json.dumps(record["evidence_ids"]),
                    ),
                )
        active = {
            run_id: (path_id, version)
            for run_id, path_id, version in db.execute(
                "SELECT run_id,path_id,version FROM path_active"
            )
        }
        for run_id, pointer in memory._active.items():
            if active.get(run_id) != pointer:
                db.execute(
                    "INSERT INTO path_active(run_id,path_id,version) VALUES(?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET "
                    "path_id=excluded.path_id,version=excluded.version",
                    (run_id, *pointer),
                )

    async def _read(self, method: str, *args, **kwargs):
        def read(db: sqlite3.Connection):
            memory = self._load_memory(db)
            return asyncio.run(getattr(memory, method)(*args, **kwargs))

        return await run_transaction(self._path, read)

    async def _mutate(self, method: str, *args, **kwargs):
        def mutate(db: sqlite3.Connection):
            memory = self._load_memory(db)
            # Staged memory methods only validate and modify detached copies.
            # No asynchronous SQLite method or nested transaction runs here.
            result = asyncio.run(getattr(memory, method)(*args, **kwargs))
            self._save_memory(db, memory)
            return result

        return await run_transaction(self._path, mutate, write=True)

    async def get_active(self, run_id: str) -> PathPlan | None:
        return await self._read("get_active", run_id)

    async def list_paths(self, run_id: str) -> list[PathPlan]:
        return await self._read("list_paths", run_id)

    async def register(
        self,
        path: PathPlan,
        expected_version: int | None = None,
        *,
        expected_path_id: str | None = None,
    ) -> PathPlan:
        # Freeze caller-owned objects before the worker is scheduled.
        return await self._mutate(
            "register",
            path.model_copy(deep=True),
            expected_version,
            expected_path_id=expected_path_id,
        )

    async def update_step(
        self,
        run_id: str,
        path_id: str,
        version: int,
        step_id: str,
        status: str,
        outcome: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> PathPlan | None:
        return await self._mutate(
            "update_step",
            run_id,
            path_id,
            version,
            step_id,
            status,
            outcome=outcome,
            evidence_ids=list(evidence_ids or []),
        )
