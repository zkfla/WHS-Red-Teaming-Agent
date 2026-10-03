"""Injected backends participate through the public change contract only."""

from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import TaskId
from redteam.orchestration.coordination import (
    ChangeSet,
    CoordinationError,
    Coordinator,
    coordinator_for,
)
from redteam.orchestration.paths.memory import InMemoryPathStore
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _PublicTaskView:
    """Public staged task view: delegates protocol surface, blocks privates."""

    def __init__(self, inner: InMemoryTaskGraphStore) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and name != "_inner":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


class _PublicPathView:
    """Public staged path view: delegates protocol surface, blocks privates."""

    def __init__(self, inner: InMemoryPathStore) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and name != "_inner":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


class ScriptedBackend:
    """Test-only provider: deepcopy staging, commit-or-rollback, no I/O."""

    def __init__(self, tasks: InMemoryTaskGraphStore, paths: InMemoryPathStore | None) -> None:
        self._tasks = tasks
        self._paths = paths
        self._records: dict = {}
        self._lock = asyncio.Lock()
        self.failures = 0

    def atomic_scope(self):
        return ("scripted-test", id(self._tasks))

    def attach_paths(self, paths) -> None:
        if self._paths is paths:
            return
        if self._paths is not None:
            raise CoordinationError("task_store_already_bound_to_another_path_store")
        if not isinstance(paths, InMemoryPathStore):
            raise CoordinationError("atomic_store_configuration_unsupported")
        self._paths = paths

    def check_history(self, history) -> None:
        return None

    async def change(self, callback):
        async with self._lock:
            staged_tasks = InMemoryTaskGraphStore()
            staged_tasks._missions = copy.deepcopy(self._tasks._missions)
            staged_tasks._tasks = copy.deepcopy(self._tasks._tasks)
            staged_paths = InMemoryPathStore()
            if self._paths is not None:
                staged_paths._snapshots = copy.deepcopy(self._paths._snapshots)
                staged_paths._progress = copy.deepcopy(self._paths._progress)
                staged_paths._active = copy.deepcopy(self._paths._active)
            staged = ChangeSet(  # type: ignore[arg-type]
                _PublicTaskView(staged_tasks),
                _PublicPathView(staged_paths),
                copy.deepcopy(self._records),
            )
            result = await callback(staged)
            if self.failures:
                self.failures -= 1
                raise RuntimeError("injected post-callback failure")
            self._tasks._missions = staged_tasks._missions
            self._tasks._tasks = staged_tasks._tasks
            if self._paths is not None:
                self._paths._snapshots = staged_paths._snapshots
                self._paths._progress = staged_paths._progress
                self._paths._active = staged_paths._active
            self._records = staged.records
            return result


def _mission(mission_id="mission-inject", engagement_id="eng-inject") -> MissionPlan:
    return MissionPlan(
        mission_id=mission_id,
        engagement_id=engagement_id,
        objective="Assess the authorized target",
        status=MissionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )


def _task(task_id="task-inject", mission_id="mission-inject") -> MissionTask:
    return MissionTask(
        task_id=task_id,
        mission_id=mission_id,
        title=task_id,
        objective="web investigation",
        status=TaskStatus.READY,
        priority=10,
        required_capability="web",
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )


async def test_injected_backend_commits_task_and_records_together() -> None:
    tasks, paths = InMemoryTaskGraphStore(), InMemoryPathStore()
    await tasks.create_mission(_mission())
    backend = ScriptedBackend(tasks, paths)
    coordinator = coordinator_for(tasks, backend=backend)

    await coordinator.bind("run-inject", "mission-inject", "eng-inject")

    async def work(change: ChangeSet) -> None:
        await change.tasks.create_task(_task())
        change.records[("generation", "run-inject:mission-inject")] = 1

    await coordinator.change(work)
    assert await tasks.get_task(TaskId("task-inject")) is not None

    async def read(change: ChangeSet):
        return dict(change.records)

    records = await coordinator.change(read)
    assert records[("generation", "run-inject:mission-inject")] == 1


async def test_injected_failure_leaves_zero_partial_application() -> None:
    tasks, paths = InMemoryTaskGraphStore(), InMemoryPathStore()
    await tasks.create_mission(_mission())
    backend = ScriptedBackend(tasks, paths)
    backend.failures = 1
    coordinator = coordinator_for(tasks, backend=backend)

    async def work(change: ChangeSet) -> None:
        await change.tasks.create_task(_task())
        change.records[("generation", "run-inject:mission-inject")] = 1

    with pytest.raises(RuntimeError, match="injected post-callback failure"):
        await coordinator.change(work)
    assert await tasks.get_task(TaskId("task-inject")) is None

    async def read(change: ChangeSet):
        return dict(change.records)

    assert ("generation", "run-inject:mission-inject") not in await coordinator.change(read)


async def test_injected_backend_is_reused_and_mismatch_is_rejected() -> None:
    tasks, paths = InMemoryTaskGraphStore(), InMemoryPathStore()
    first = ScriptedBackend(tasks, paths)
    assert coordinator_for(tasks, backend=first) is coordinator_for(tasks)
    assert coordinator_for(tasks, backend=first) is coordinator_for(tasks, paths)
    with pytest.raises(CoordinationError, match="atomic_state_backend_mismatched"):
        coordinator_for(tasks, backend=ScriptedBackend(tasks, paths))


async def test_consumer_sees_only_public_staged_view_but_bind_and_rollback_work() -> None:
    tasks, paths = InMemoryTaskGraphStore(), InMemoryPathStore()
    await tasks.create_mission(_mission())
    backend = ScriptedBackend(tasks, paths)
    coordinator = coordinator_for(tasks, backend=backend)
    await coordinator.bind("run-inject", "mission-inject", "eng-inject")

    async def probe(change: ChangeSet) -> str:
        for private in ("_missions", "_tasks", "_lock"):
            assert not hasattr(change.tasks, private), private
        for private in ("_snapshots", "_progress", "_active", "_lock"):
            assert not hasattr(change.paths, private), private
        assert not isinstance(change.tasks, InMemoryTaskGraphStore)
        assert not isinstance(change.paths, InMemoryPathStore)
        await change.tasks.create_task(_task("task-view"))
        snapshot = change.tasks.snapshot_tasks()
        await change.tasks.create_task(_task("task-view-2"))
        change.tasks.restore_tasks(snapshot)
        listed = await change.tasks.list_tasks()
        assert {item.task_id for item in listed} == {"task-view"}
        return "ok"

    assert await coordinator.change(probe) == "ok"
    assert await tasks.get_task(TaskId("task-view")) is not None
    assert await tasks.get_task(TaskId("task-view-2")) is None

    backend.failures = 1

    async def doomed(change: ChangeSet) -> None:
        await change.tasks.create_task(_task("task-doomed"))

    with pytest.raises(RuntimeError, match="injected post-callback failure"):
        await coordinator.change(doomed)
    assert await tasks.get_task(TaskId("task-doomed")) is None


def test_coordinator_consumes_no_concrete_store_types() -> None:
    source = Path("src/redteam/orchestration/coordination.py").read_text()
    for private in (
        r"task_graph\.sqlite",
        r"task_graph\.memory",
        r"paths\.sqlite",
        r"paths\.memory",
        r"operator_intents",
        r"(?<![A-Za-z0-9_])_missions(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_tasks(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_snapshots(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_progress(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_active(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_lock(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_connect(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_load_memory(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_])_save_memory(?![A-Za-z0-9_])",
        r"_orchestration_coordinator",
    ):
        assert not re.search(private, source), private


def test_coordinator_accepts_a_bare_protocol_backend() -> None:
    assert isinstance(Coordinator(ScriptedBackend(InMemoryTaskGraphStore(), None)), Coordinator)
