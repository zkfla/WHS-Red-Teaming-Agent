"""Composite atomic state providers (Task/Path/records in one boundary).

The coordinator consumes only the public change session. Each provider owns
its own storage, concurrency and records persistence:

- memory: one shared lock, deepcopy staging, no-await swap;
- SQLite: one connection under BEGIN IMMEDIATE, snapshot load, callback,
  changed-row save plus records upsert, single commit or rollback.

Callbacks see detached memory views only; no outside I/O (LLM, tools or
publishing) may run inside them.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Coroutine, Hashable
from typing import Any, TypeVar

from redteam.orchestration.coordination import (
    AtomicStateBackend,
    ChangeSet,
    CoordinationError,
)
from redteam.orchestration.paths.memory import InMemoryPathStore
from redteam.orchestration.sqlite_support import run_sqlite
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore

T = TypeVar("T")

SCHEMA_VERSION = 1


class MemoryStateBackend:
    """In-memory composite provider sharing one lock across its members."""

    def __init__(self, tasks: InMemoryTaskGraphStore, paths: InMemoryPathStore | None) -> None:
        self._tasks = tasks
        self._paths = paths
        self._records: dict[tuple[str, str], Any] = {}
        self._lock = asyncio.Lock()
        tasks._lock = self._lock
        if paths is not None:
            paths._lock = self._lock

    def atomic_scope(self) -> Hashable:
        return ("memory", id(self._tasks))

    @property
    def paths(self) -> InMemoryPathStore | None:
        return self._paths

    def attach_paths(self, paths: InMemoryPathStore) -> None:
        if self._paths is paths:
            return
        if self._paths is not None:
            raise CoordinationError("task_store_already_bound_to_another_path_store")
        if not isinstance(paths, InMemoryPathStore):
            raise CoordinationError("atomic_store_configuration_unsupported")
        paths._lock = self._lock
        self._paths = paths

    def check_history(self, history: Any | None) -> None:
        if history is None:
            return
        from redteam.orchestration.operator_intents.store import InMemoryOperatorIntentStore

        if not isinstance(history, InMemoryOperatorIntentStore):
            raise CoordinationError("atomic_intent_history_configuration_unsupported")

    async def change(self, callback: Callable[[ChangeSet], Coroutine[Any, Any, T]]) -> T:
        async with self._lock:
            staged_tasks = InMemoryTaskGraphStore()
            staged_tasks._missions = copy.deepcopy(self._tasks._missions)
            staged_tasks._tasks = copy.deepcopy(self._tasks._tasks)
            staged_paths = InMemoryPathStore()
            if self._paths is not None:
                staged_paths._snapshots = copy.deepcopy(self._paths._snapshots)
                staged_paths._progress = copy.deepcopy(self._paths._progress)
                staged_paths._active = copy.deepcopy(self._paths._active)
            staged = ChangeSet(staged_tasks, staged_paths, copy.deepcopy(self._records))
            result = await callback(staged)
            # No await between commits: readers and public mutations use lock.
            self._tasks._missions, self._tasks._tasks = staged_tasks._missions, staged_tasks._tasks
            if self._paths is not None:
                self._paths._snapshots = staged_paths._snapshots
                self._paths._progress, self._paths._active = (
                    staged_paths._progress,
                    staged_paths._active,
                )
            self._records = staged.records
            return result


class SqliteStateBackend:
    """SQLite composite provider: one connection, one commit or rollback."""

    def __init__(self, tasks: Any, paths: Any | None) -> None:
        self._tasks = tasks
        self._paths = paths
        self._lock = asyncio.Lock()

    def atomic_scope(self) -> Hashable:
        return ("sqlite", str(self._tasks._path.resolve()))

    @property
    def paths(self) -> Any | None:
        return self._paths

    def attach_paths(self, paths: Any) -> None:
        if self._paths is paths:
            return
        if self._paths is not None:
            raise CoordinationError("task_store_already_bound_to_another_path_store")
        from redteam.orchestration.paths.sqlite import SQLitePathStore

        if not isinstance(paths, SQLitePathStore) or (
            paths._path.resolve() != self._tasks._path.resolve()
        ):
            raise CoordinationError("atomic_store_configuration_unsupported")
        self._paths = paths

    def check_history(self, history: Any | None) -> None:
        if history is None:
            return
        from redteam.orchestration.operator_intents.store import SQLiteOperatorIntentStore

        if not isinstance(history, SQLiteOperatorIntentStore):
            raise CoordinationError("atomic_intent_history_configuration_unsupported")

    def _transaction(self, callback: Callable[[ChangeSet], Coroutine[Any, Any, T]]) -> T:
        from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

        db = self._tasks._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS orchestration_migrations(version INTEGER PRIMARY KEY)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS orchestration_records("
                "kind TEXT NOT NULL,identity TEXT NOT NULL,payload TEXT NOT NULL,"
                "PRIMARY KEY(kind,identity))"
            )
            version = db.execute("SELECT MAX(version) FROM orchestration_migrations").fetchone()[0]
            if version is not None and version > SCHEMA_VERSION:
                raise CoordinationError("unsupported_orchestration_schema_version")
            db.execute("INSERT OR IGNORE INTO orchestration_migrations VALUES(1)")
            from redteam.orchestration.paths.sqlite import SQLitePathStore

            staged_tasks = SQLiteTaskGraphStore._load_memory(db)
            staged_paths = (
                SQLitePathStore._load_memory(db)
                if self._paths is not None
                else InMemoryPathStore()
            )
            staged = ChangeSet(
                staged_tasks,
                staged_paths,
                {
                    (kind, identity): json.loads(payload)
                    for kind, identity, payload in db.execute(
                        "SELECT kind,identity,payload FROM orchestration_records"
                    )
                },
            )
            # Only detached memory validation/mutation is awaited in this private
            # worker loop. No database methods, LLM, publisher or tool are called.
            result = asyncio.run(callback(staged))
            SQLiteTaskGraphStore._save_memory(db, staged_tasks)
            if self._paths is not None:
                SQLitePathStore._save_memory(db, staged_paths)
            for (kind, identity), record in staged.records.items():
                db.execute(
                    "INSERT INTO orchestration_records VALUES(?,?,?) ON CONFLICT(kind,identity) "
                    "DO UPDATE SET payload=excluded.payload",
                    (kind, identity, json.dumps(record)),
                )
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def change(self, callback: Callable[[ChangeSet], Coroutine[Any, Any, T]]) -> T:
        await self._tasks.setup()
        async with self._lock:
            return await run_sqlite(lambda: self._transaction(callback))


def state_backend_for(tasks: Any, paths: Any | None = None) -> AtomicStateBackend:
    """Choose the single composite provider once at assembly time."""
    existing = getattr(tasks, "_coordination_backend", None)
    if existing is not None:
        if paths is not None:
            existing.attach_paths(paths)
        return existing
    from redteam.orchestration.paths.sqlite import SQLitePathStore
    from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

    if isinstance(tasks, InMemoryTaskGraphStore):
        if paths is not None and not isinstance(paths, InMemoryPathStore):
            raise CoordinationError("atomic_store_configuration_unsupported")
        backend: AtomicStateBackend = MemoryStateBackend(tasks, paths)
        # setattr keeps the builtin store classes unchanged; mypy rejects a
        # direct attribute write and B010 is the accepted trade.
        setattr(tasks, "_coordination_backend", backend)  # noqa: B010
        return backend
    if isinstance(tasks, SQLiteTaskGraphStore):
        if paths is not None and (
            not isinstance(paths, SQLitePathStore) or tasks._path.resolve() != paths._path.resolve()
        ):
            raise CoordinationError("atomic_store_configuration_unsupported")
        backend = SqliteStateBackend(tasks, paths)
        setattr(tasks, "_coordination_backend", backend)  # noqa: B010
        return backend
    raise CoordinationError("atomic_store_configuration_unsupported")
