"""Durable SQLite Mission/Task Graph store, separate from the Work Ledger."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from redteam.core.contracts.mission import MissionPlan, MissionTask, redact_secret_material
from redteam.core.enums import TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.sqlite_support import connect, run_sqlite, run_transaction


class SQLiteTaskGraphStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._ready = False
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        """Connection helper for the coordinator; call and close in its worker thread."""
        return connect(self._path, write=True)

    async def setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return

            def initialize() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                db = self._connect()
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("BEGIN IMMEDIATE")
                    self._create_schema(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()

            await run_sqlite(initialize)
            self._ready = True

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        # Execute separately: executescript would implicitly commit the outer transaction.
        for statement in (
            """CREATE TABLE IF NOT EXISTS missions (
                mission_id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, payload TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_missions_engagement
                ON missions(engagement_id,mission_id)""",
            """CREATE TABLE IF NOT EXISTS mission_tasks (
                task_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, status TEXT NOT NULL,
                priority INTEGER NOT NULL, payload TEXT NOT NULL,
                FOREIGN KEY(mission_id) REFERENCES missions(mission_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_tasks_mission_status_priority
                ON mission_tasks(mission_id,status,priority DESC,task_id)""",
        ):
            db.execute(statement)

    @staticmethod
    def _load_memory(db: sqlite3.Connection) -> InMemoryTaskGraphStore:
        """Read a fresh validation snapshot using the caller's active transaction."""
        memory = InMemoryTaskGraphStore()
        missions = [
            MissionPlan.model_validate(redact_secret_material(json.loads(row[0])))
            for row in db.execute("SELECT payload FROM missions")
        ]
        tasks = [
            MissionTask.model_validate(redact_secret_material(json.loads(row[0])))
            for row in db.execute("SELECT payload FROM mission_tasks")
        ]
        memory._missions = {item.mission_id: item for item in missions}
        memory._tasks = {item.task_id: item for item in tasks}
        return memory

    @staticmethod
    def _save_memory(db: sqlite3.Connection, memory: InMemoryTaskGraphStore) -> None:
        """Persist changed rows only, without opening or committing a transaction.

        The coordinator supplies a snapshot loaded inside this same transaction.
        No cached snapshot, unrelated row, or other orchestration table is deleted.
        """
        missions = dict(db.execute("SELECT mission_id,payload FROM missions"))
        tasks = dict(db.execute("SELECT task_id,payload FROM mission_tasks"))
        for mission in memory._missions.values():
            payload = mission.redacted_copy().model_dump_json()
            if missions.get(str(mission.mission_id)) != payload:
                db.execute(
                    """INSERT INTO missions(mission_id,engagement_id,payload) VALUES(?,?,?)
                    ON CONFLICT(mission_id) DO UPDATE SET
                    engagement_id=excluded.engagement_id,payload=excluded.payload""",
                    (str(mission.mission_id), str(mission.engagement_id), payload),
                )
        for task in memory._tasks.values():
            payload = task.redacted_copy().model_dump_json()
            if tasks.get(str(task.task_id)) != payload:
                db.execute(
                    """INSERT INTO mission_tasks(task_id,mission_id,status,priority,payload)
                    VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                    mission_id=excluded.mission_id,status=excluded.status,
                    priority=excluded.priority,payload=excluded.payload""",
                    (
                        str(task.task_id),
                        str(task.mission_id),
                        task.status.value,
                        task.priority,
                        payload,
                    ),
                )

    async def _mutate(self, method: str, *args, **kwargs):
        await self.setup()

        def mutate(db: sqlite3.Connection):
            memory = self._load_memory(db)
            # These staged memory methods only run existing domain rules against local copies.
            # They never call an asynchronous SQLite store or open a nested transaction.
            result = asyncio.run(getattr(memory, method)(*args, **kwargs))
            self._save_memory(db, memory)
            return result

        return await run_transaction(self._path, mutate, write=True)

    async def _read(self, method: str, *args, **kwargs):
        await self.setup()

        def read(db: sqlite3.Connection):
            memory = self._load_memory(db)
            return asyncio.run(getattr(memory, method)(*args, **kwargs))

        return await run_transaction(self._path, read)

    async def create_mission(self, mission: MissionPlan) -> None:
        await self._mutate("create_mission", mission)

    async def update_mission(self, mission: MissionPlan) -> None:
        await self._mutate("update_mission", mission)

    async def create_task(self, task: MissionTask) -> None:
        await self._mutate("create_task", task)

    async def update_task(self, task: MissionTask) -> None:
        await self._mutate("update_task", task)

    async def transition_task(
        self, task_id: TaskId, status: TaskStatus, *, now: datetime, reason: str | None = None
    ) -> MissionTask:
        return await self._mutate("transition_task", task_id, status, now=now, reason=reason)

    async def add_dependency(
        self, task_id: TaskId, dependency_id: TaskId, *, now: datetime
    ) -> MissionTask:
        return await self._mutate("add_dependency", task_id, dependency_id, now=now)

    async def update_priority(
        self, task_id: TaskId, priority: int, *, now: datetime
    ) -> MissionTask:
        return await self._mutate("update_priority", task_id, priority, now=now)

    async def block_task(self, task_id: TaskId, reason: str, *, now: datetime) -> MissionTask:
        return await self._mutate("block_task", task_id, reason, now=now)

    async def unblock_task(self, task_id: TaskId, *, now: datetime) -> MissionTask:
        return await self._mutate("unblock_task", task_id, now=now)

    async def split_task(self, parent_id: TaskId, children: list[MissionTask]) -> list[MissionTask]:
        return await self._mutate("split_task", parent_id, children)

    async def get_mission(self, mission_id: MissionId) -> MissionPlan | None:
        return await self._read("get_mission", mission_id)

    async def list_missions(
        self, *, engagement_id: EngagementId | None = None
    ) -> list[MissionPlan]:
        return await self._read("list_missions", engagement_id=engagement_id)

    async def get_task(self, task_id: TaskId) -> MissionTask | None:
        return await self._read("get_task", task_id)

    async def list_tasks(
        self, *, mission_id: MissionId | None = None, status: TaskStatus | None = None
    ) -> list[MissionTask]:
        return await self._read("list_tasks", mission_id=mission_id, status=status)
