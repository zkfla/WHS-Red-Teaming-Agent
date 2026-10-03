"""Concurrency-safe in-memory Mission and Task Graph store."""

from __future__ import annotations

import asyncio
from datetime import datetime

from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.enums import TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.task_graph.rules import (
    DuplicateTaskError,
    InvalidDependency,
    InvalidTaskTransition,
    TaskNotFoundError,
    dependencies_completed,
    validate_dependencies,
    validate_transition,
)


class InMemoryTaskGraphStore:
    def __init__(self) -> None:
        self._missions: dict[MissionId, MissionPlan] = {}
        self._tasks: dict[TaskId, MissionTask] = {}
        self._lock = asyncio.Lock()

    async def create_mission(self, mission: MissionPlan) -> None:
        async with self._lock:
            if mission.mission_id in self._missions:
                raise DuplicateTaskError(f"mission exists: {mission.mission_id}")
            self._missions[mission.mission_id] = mission.redacted_copy()

    async def get_mission(self, mission_id: MissionId) -> MissionPlan | None:
        async with self._lock:
            value = self._missions.get(mission_id)
            return value.redacted_copy() if value else None

    def peek_mission(self, mission_id: MissionId) -> MissionPlan | None:
        """Lock-free staged read for the atomic backend.

        The caller already holds the backend boundary (shared lock or
        transaction snapshot), so no locking or copying happens here.
        """
        return self._missions.get(mission_id)

    def peek_tasks_of_mission(self, mission_id: MissionId) -> list[MissionTask]:
        """Lock-free staged task list for one mission (same boundary rule)."""
        return [task for task in self._tasks.values() if str(task.mission_id) == str(mission_id)]

    def snapshot_tasks(
        self,
    ) -> tuple[dict[MissionId, MissionPlan], dict[TaskId, MissionTask]]:
        """Copy staged mission/task state for one bounded rollback.

        The caller already holds the backend boundary, so no locking
        happens here. Each entry is an independent copy; later staged
        mutations never leak into the snapshot. The snapshot is consumed
        by restore_tasks and must not be reused afterwards.
        """
        return (
            {key: item.model_copy(deep=True) for key, item in self._missions.items()},
            {key: item.model_copy(deep=True) for key, item in self._tasks.items()},
        )

    def restore_tasks(
        self,
        snapshot: tuple[dict[MissionId, MissionPlan], dict[TaskId, MissionTask]],
    ) -> None:
        """Roll staged mission/task state back to a snapshot_tasks copy."""
        missions, tasks = snapshot
        self._missions = missions
        self._tasks = tasks

    async def list_missions(
        self, *, engagement_id: EngagementId | None = None
    ) -> list[MissionPlan]:
        async with self._lock:
            return [
                v.redacted_copy()
                for v in sorted(self._missions.values(), key=lambda x: str(x.mission_id))
                if engagement_id is None or v.engagement_id == engagement_id
            ]

    async def update_mission(self, mission: MissionPlan) -> None:
        async with self._lock:
            old = self._missions.get(mission.mission_id)
            if old is None:
                raise TaskNotFoundError(f"mission not found: {mission.mission_id}")
            if old.engagement_id != mission.engagement_id:
                raise ValueError("engagement_id is immutable")
            if mission.revision != old.revision + 1:
                raise ValueError("mission revision must increase by exactly one")
            self._missions[mission.mission_id] = mission.redacted_copy()

    async def create_task(self, task: MissionTask) -> None:
        async with self._lock:
            self._create_task(task)

    def _create_task(self, task: MissionTask) -> None:
        if task.task_id in self._tasks:
            raise DuplicateTaskError(f"task exists: {task.task_id}")
        if task.mission_id not in self._missions:
            raise TaskNotFoundError(f"mission not found: {task.mission_id}")
        if task.parent_task_id is not None:
            parent = self._tasks.get(task.parent_task_id)
            if parent is None or parent.mission_id != task.mission_id:
                raise InvalidDependency("parent must exist in the same mission")
        validate_dependencies(task, self._tasks)
        if task.status in {TaskStatus.READY, TaskStatus.RUNNING} and not dependencies_completed(
            task, self._tasks
        ):
            raise InvalidDependency("task cannot be ready before dependencies complete")
        self._tasks[task.task_id] = task.redacted_copy()

    async def get_task(self, task_id: TaskId) -> MissionTask | None:
        async with self._lock:
            value = self._tasks.get(task_id)
            return value.redacted_copy() if value else None

    async def list_tasks(
        self, *, mission_id: MissionId | None = None, status: TaskStatus | None = None
    ) -> list[MissionTask]:
        async with self._lock:
            values = [
                v
                for v in self._tasks.values()
                if (mission_id is None or v.mission_id == mission_id)
                and (status is None or v.status is status)
            ]
            return [
                v.redacted_copy()
                for v in sorted(values, key=lambda x: (-x.priority, str(x.task_id)))
            ]

    async def update_task(self, task: MissionTask) -> None:
        async with self._lock:
            old = self._require(task.task_id)
            if old.mission_id != task.mission_id or old.created_at != task.created_at:
                raise ValueError("task identity fields are immutable")
            if old.status is not task.status:
                raise InvalidTaskTransition("update_task cannot bypass lifecycle transitions")
            if old.parent_task_id != task.parent_task_id:
                raise ValueError("parent_task_id is immutable")
            if old.dependency_task_ids != task.dependency_task_ids:
                raise InvalidDependency("use add_dependency to change dependencies")
            validate_dependencies(task, self._tasks)
            self._tasks[task.task_id] = task.redacted_copy()

    async def transition_task(
        self, task_id: TaskId, status: TaskStatus, *, now: datetime, reason: str | None = None
    ) -> MissionTask:
        async with self._lock:
            task = self._require(task_id)
            validate_transition(task.status, status)
            if status in {TaskStatus.READY, TaskStatus.RUNNING} and not dependencies_completed(
                task, self._tasks
            ):
                raise InvalidDependency("dependencies are not completed")
            if status is TaskStatus.BLOCKED and not (reason or "").strip():
                raise ValueError("blocking reason is required")
            updated = task.model_copy(
                update={
                    "status": status,
                    "blocking_reason": reason.strip()
                    if status is TaskStatus.BLOCKED and reason
                    else None,
                    "updated_at": now,
                }
            ).redacted_copy()
            self._tasks[task_id] = updated
            return updated.model_copy(deep=True)

    async def add_dependency(
        self, task_id: TaskId, dependency_id: TaskId, *, now: datetime
    ) -> MissionTask:
        async with self._lock:
            task = self._require(task_id)
            self._require(dependency_id)
            if task.status in {
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                raise InvalidDependency("cannot alter dependencies after task starts")
            updated = task.model_copy(
                update={
                    "dependency_task_ids": sorted(
                        set(task.dependency_task_ids + [dependency_id]), key=str
                    ),
                    "status": TaskStatus.PENDING
                    if task.status is TaskStatus.READY
                    else task.status,
                    "updated_at": now,
                }
            ).redacted_copy()
            validate_dependencies(updated, self._tasks)
            self._tasks[task_id] = updated
            return updated.model_copy(deep=True)

    async def update_priority(
        self, task_id: TaskId, priority: int, *, now: datetime
    ) -> MissionTask:
        async with self._lock:
            task = self._require(task_id)
            updated = task.model_copy(
                update={"priority": priority, "updated_at": now}
            ).redacted_copy()
            self._tasks[task_id] = updated
            return updated.model_copy(deep=True)

    async def block_task(self, task_id: TaskId, reason: str, *, now: datetime) -> MissionTask:
        return await self.transition_task(task_id, TaskStatus.BLOCKED, now=now, reason=reason)

    async def unblock_task(self, task_id: TaskId, *, now: datetime) -> MissionTask:
        async with self._lock:
            task = self._require(task_id)
            target = (
                TaskStatus.READY
                if dependencies_completed(task, self._tasks)
                else TaskStatus.PENDING
            )
            validate_transition(task.status, target)
            updated = task.model_copy(
                update={"status": target, "blocking_reason": None, "updated_at": now}
            ).redacted_copy()
            self._tasks[task_id] = updated
            return updated.model_copy(deep=True)

    async def split_task(self, parent_id: TaskId, children: list[MissionTask]) -> list[MissionTask]:
        async with self._lock:
            parent = self._require(parent_id)
            if not children:
                raise ValueError("split requires at least one child")
            staged = dict(self._tasks)
            for child in children:
                if child.parent_task_id != parent_id or child.mission_id != parent.mission_id:
                    raise InvalidDependency(
                        "split children must reference their parent and mission"
                    )
                if child.task_id in staged:
                    raise DuplicateTaskError(f"task exists: {child.task_id}")
                child = child.redacted_copy()
                validate_dependencies(child, staged)
                staged[child.task_id] = child
            self._tasks = staged
            return [staged[child.task_id].model_copy(deep=True) for child in children]

    def _require(self, task_id: TaskId) -> MissionTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task
