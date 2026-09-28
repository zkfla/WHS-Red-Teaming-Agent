"""Durable SQLite Mission/Task Graph store, separate from the Work Ledger."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.enums import TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore


class SQLiteTaskGraphStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._memory = InMemoryTaskGraphStore()
        self._ready = False
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path) as db:
                db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS missions (
                    mission_id TEXT PRIMARY KEY,
                    engagement_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_missions_engagement
                    ON missions(engagement_id, mission_id);
                CREATE TABLE IF NOT EXISTS mission_tasks (
                    task_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(mission_id) REFERENCES missions(mission_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_mission_status_priority
                    ON mission_tasks(mission_id,status,priority DESC,task_id);
                """)
                missions = [
                    MissionPlan.model_validate_json(row[0])
                    for row in db.execute("SELECT payload FROM missions")
                ]
                tasks = [
                    MissionTask.model_validate_json(row[0])
                    for row in db.execute("SELECT payload FROM mission_tasks")
                ]
            self._memory._missions = {item.mission_id: item for item in missions}
            self._memory._tasks = {item.task_id: item for item in tasks}
            self._ready = True

    def _flush(self) -> None:
        with sqlite3.connect(self._path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM mission_tasks")
            db.execute("DELETE FROM missions")
            db.executemany(
                "INSERT INTO missions VALUES(?,?,?)",
                [
                    (str(v.mission_id), str(v.engagement_id), v.model_dump_json())
                    for v in self._memory._missions.values()
                ],
            )
            db.executemany(
                "INSERT INTO mission_tasks VALUES(?,?,?,?,?)",
                [
                    (
                        str(v.task_id),
                        str(v.mission_id),
                        v.status.value,
                        v.priority,
                        v.model_dump_json(),
                    )
                    for v in self._memory._tasks.values()
                ],
            )
            db.commit()

    async def _mutate(self, method: str, *args, **kwargs):
        await self.setup()
        async with self._lock:
            result = await getattr(self._memory, method)(*args, **kwargs)
            self._flush()
            return result

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
        await self.setup()
        return await self._memory.get_mission(mission_id)

    async def list_missions(
        self, *, engagement_id: EngagementId | None = None
    ) -> list[MissionPlan]:
        await self.setup()
        return await self._memory.list_missions(engagement_id=engagement_id)

    async def get_task(self, task_id: TaskId) -> MissionTask | None:
        await self.setup()
        return await self._memory.get_task(task_id)

    async def list_tasks(
        self, *, mission_id: MissionId | None = None, status: TaskStatus | None = None
    ) -> list[MissionTask]:
        await self.setup()
        return await self._memory.list_tasks(mission_id=mission_id, status=status)
