"""Atomic orchestration changes; external I/O is always outside this boundary.

Callbacks operate exclusively on detached memory stores. SQLite runs the same
calculation on its owning thread and commits task, path and internal records
with one connection. The intent history is a recoverable audit projection.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar

from redteam.core.enums import MissionStatus
from redteam.core.ids import MissionId
from redteam.orchestration.paths.memory import InMemoryPathStore
from redteam.orchestration.paths.models import PathVersionConflict
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore

T = TypeVar("T")
CLOSED = frozenset({MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED})
BLOCKING_INTENTS = frozenset({"stop", "pause", "resume", "focus", "reprioritize", "exclude"})


class CoordinationError(ValueError):
    """Safe, code-owned orchestration rejection."""


class StateConflict(PathVersionConflict):
    """A reviewed snapshot no longer describes the canonical state."""


def content_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class ChangeSet:
    tasks: InMemoryTaskGraphStore
    paths: InMemoryPathStore
    records: dict[tuple[str, str], Any] = field(default_factory=dict)

    def binding(self, run_id: str, mission_id: str, engagement_id: str | None = None) -> dict:
        bound = self.records.get(("binding", run_id))
        if bound is None or bound["mission_id"] != mission_id:
            raise CoordinationError("run_binding_missing_or_mismatched")
        if engagement_id is not None and bound["engagement_id"] != engagement_id:
            raise CoordinationError("engagement_binding_mismatched")
        return bound

    def legacy_scope(self, run_id: str) -> None:
        if ("binding", run_id) in self.records:
            raise CoordinationError("bound_run_requires_mission_identity")

    def generation(self, run_id: str, mission_id: str) -> int:
        return self.records.get(("generation", f"{run_id}:{mission_id}"), 0)

    def pending(self, run_id: str, mission_id: str) -> bool:
        return any(
            kind == "intent"
            and item["run_id"] == run_id
            and item["intent"]["mission_id"] == mission_id
            and item["status"] == "received"
            and item["intent"]["intent_type"] in BLOCKING_INTENTS
            for (kind, _), item in self.records.items()
        )

    def token(self, run_id: str, mission_id: str) -> str:
        mission = self.tasks._missions.get(MissionId(mission_id))
        pointer = self.paths._active.get(run_id)
        path = self.paths._snapshots.get((run_id, *pointer)) if pointer else None
        if path is not None:
            path = self.paths._merged(path)
        tasks = sorted(
            (
                item.model_dump(mode="json")
                for item in self.tasks._tasks.values()
                if str(item.mission_id) == mission_id
            ),
            key=lambda item: item["task_id"],
        )
        return content_hash(
            {
                "mission": mission.model_dump(mode="json") if mission else None,
                "binding": self.records.get(("binding", run_id)),
                "path": path.model_dump(mode="json") if path else None,
                "tasks": tasks,
                "generation": self.generation(run_id, mission_id),
            }
        )

    def admit(self, run_id: str, mission_id: str, *, token: str | None = None) -> None:
        bound = self.binding(run_id, mission_id)
        mission = self.tasks._missions.get(MissionId(mission_id))
        if bound["closed"] or mission is None or mission.status is not MissionStatus.ACTIVE:
            raise CoordinationError("mission_not_active")
        if self.pending(run_id, mission_id):
            raise StateConflict("pending_operator_mutation")
        if token is not None and self.token(run_id, mission_id) != token:
            raise StateConflict("reviewed_state_changed")

    def queue(self, envelope: Any) -> None:
        key = ("outbox", str(envelope.event_id))
        if key in self.records:
            return
        counter_key = ("outbox_sequence", str(envelope.run_id))
        sequence = self.records.get(counter_key, 0) + 1
        self.records[counter_key] = sequence
        self.records[key] = {
            "sequence": sequence,
            "envelope": envelope.model_dump(mode="json"),
            "delivered": False,
            "owner": None,
            "lease_until": 0.0,
        }


class Coordinator:
    def __init__(self, tasks: Any, paths: Any | None) -> None:
        from redteam.orchestration.paths.sqlite import SQLitePathStore
        from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

        self.tasks = tasks
        self.paths = paths
        self.lock = asyncio.Lock()
        self.records: dict[tuple[str, str], Any] = {}
        if isinstance(tasks, InMemoryTaskGraphStore):
            if paths is not None and not isinstance(paths, InMemoryPathStore):
                raise CoordinationError("atomic_store_configuration_unsupported")
            self.sqlite = False
            tasks._lock = self.lock
            if paths is not None:
                paths._lock = self.lock
        elif isinstance(tasks, SQLiteTaskGraphStore):
            if paths is not None and (
                not isinstance(paths, SQLitePathStore)
                or tasks._path.resolve() != paths._path.resolve()
            ):
                raise CoordinationError("atomic_store_configuration_unsupported")
            self.sqlite = True
        else:
            raise CoordinationError("atomic_store_configuration_unsupported")

    def attach_paths(self, paths: Any) -> None:
        if self.paths is paths:
            return
        if self.paths is not None:
            raise CoordinationError("task_store_already_bound_to_another_path_store")
        from redteam.orchestration.paths.sqlite import SQLitePathStore

        if self.sqlite:
            if (
                not isinstance(paths, SQLitePathStore)
                or paths._path.resolve() != self.tasks._path.resolve()
            ):
                raise CoordinationError("atomic_store_configuration_unsupported")
        else:
            if not isinstance(paths, InMemoryPathStore):
                raise CoordinationError("atomic_store_configuration_unsupported")
            paths._lock = self.lock
        self.paths = paths

    def _transaction(self, callback: Callable[[ChangeSet], Coroutine[Any, Any, T]]) -> T:
        from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

        db = self.tasks._connect()
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
            if version is not None and version > 1:
                raise CoordinationError("unsupported_orchestration_schema_version")
            db.execute("INSERT OR IGNORE INTO orchestration_migrations VALUES(1)")
            from redteam.orchestration.paths.sqlite import SQLitePathStore

            staged = ChangeSet(
                SQLiteTaskGraphStore._load_memory(db),
                SQLitePathStore._load_memory(db) if self.paths is not None else InMemoryPathStore(),
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
            SQLiteTaskGraphStore._save_memory(db, staged.tasks)
            if self.paths is not None:
                SQLitePathStore._save_memory(db, staged.paths)
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
        if self.sqlite:
            await self.tasks.setup()
            from redteam.orchestration.sqlite_support import run_sqlite

            async with self.lock:
                return await run_sqlite(lambda: self._transaction(callback))
        async with self.lock:
            staged_tasks = InMemoryTaskGraphStore()
            staged_tasks._missions = copy.deepcopy(self.tasks._missions)
            staged_tasks._tasks = copy.deepcopy(self.tasks._tasks)
            staged_paths = InMemoryPathStore()
            if self.paths is not None:
                staged_paths._snapshots = copy.deepcopy(self.paths._snapshots)
                staged_paths._progress = copy.deepcopy(self.paths._progress)
                staged_paths._active = copy.deepcopy(self.paths._active)
            staged = ChangeSet(staged_tasks, staged_paths, copy.deepcopy(self.records))
            result = await callback(staged)
            # No await between commits: readers and public mutations use lock.
            self.tasks._missions, self.tasks._tasks = staged.tasks._missions, staged.tasks._tasks
            if self.paths is not None:
                self.paths._snapshots = staged.paths._snapshots
                self.paths._progress, self.paths._active = (
                    staged.paths._progress,
                    staged.paths._active,
                )
            self.records = staged.records
            return result

    async def bind(self, run_id: str, mission_id: str, engagement_id: str) -> None:
        async def register(change: ChangeSet) -> None:
            mission = change.tasks._missions.get(MissionId(mission_id))
            if mission is None or str(mission.engagement_id) != engagement_id:
                raise CoordinationError("run_binding_mismatched")
            existing = change.records.get(("binding", run_id))
            expected = {
                "mission_id": mission_id,
                "engagement_id": engagement_id,
                "closed": mission.status in CLOSED,
            }
            if existing is not None:
                if (
                    existing["mission_id"] != mission_id
                    or existing["engagement_id"] != engagement_id
                ):
                    raise CoordinationError("run_binding_mismatched")
                return
            if any(
                kind == "binding" and bound["mission_id"] == mission_id
                for (kind, _), bound in change.records.items()
            ):
                raise CoordinationError("mission_already_bound_to_another_run")
            change.records[("binding", run_id)] = expected

        await self.change(register)

    async def review_token(self, run_id: str, mission_id: str) -> str:
        async def capture(change: ChangeSet) -> str:
            change.admit(run_id, mission_id)
            return change.token(run_id, mission_id)

        return await self.change(capture)


def coordinator_for(tasks: Any, paths: Any | None = None) -> Coordinator:
    coordinator = getattr(tasks, "_orchestration_coordinator", None)
    if coordinator is None:
        coordinator = Coordinator(tasks, paths)
        tasks._orchestration_coordinator = coordinator
    elif paths is not None:
        coordinator.attach_paths(paths)
    return coordinator


def validate_configuration(deps: Any) -> Coordinator:
    from redteam.orchestration.operator_intents.store import (
        InMemoryOperatorIntentStore,
        SQLiteOperatorIntentStore,
    )

    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    expected = SQLiteOperatorIntentStore if coordinator.sqlite else InMemoryOperatorIntentStore
    history = getattr(deps, "operator_intent_store", None)
    if history is not None and not isinstance(history, expected):
        raise CoordinationError("atomic_intent_history_configuration_unsupported")
    return coordinator
