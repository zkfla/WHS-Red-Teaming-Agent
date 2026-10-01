"""Mission/Task/Intent contracts and both Task Graph backends."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from redteam.app.bootstrap import load_engagement
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.mission import MissionPlan, MissionTask, OperatorIntent, Provenance
from redteam.core.enums import AgentPhase, MissionStatus, OperatorIntentType, TaskStatus
from redteam.core.ids import EngagementId, EventId, MissionId, OperatorIntentId, RunId, TaskId
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph import (
    InMemoryTaskGraphStore,
    InvalidDependency,
    InvalidTaskTransition,
    SQLiteTaskGraphStore,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def mission(revision: int = 1) -> MissionPlan:
    return MissionPlan(
        mission_id=MissionId("mission-1"),
        engagement_id=EngagementId("eng-1"),
        objective="Assess authorization",
        priorities=["authorization"],
        success_criteria=["evidence-backed result"],
        initial_workstreams=["API assessment"],
        assumptions=["test tenant available"],
        operator_constraints={"credential_ref": "vault://eng/tester"},
        operator_preferences={"focus": "API"},
        status=MissionStatus.ACTIVE,
        revision=revision,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=revision - 1),
        provenance=Provenance(source="operator"),
    )


def task(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    dependencies: list[str] | None = None,
    parent: str | None = None,
    priority: int = 0,
) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MissionId("mission-1"),
        title=task_id,
        objective=f"perform {task_id}",
        status=status,
        priority=priority,
        parent_task_id=TaskId(parent) if parent else None,
        dependency_task_ids=[TaskId(v) for v in dependencies or []],
        blocking_reason="waiting" if status is TaskStatus.BLOCKED else None,
        provenance=Provenance(source="planner"),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryTaskGraphStore()
    return SQLiteTaskGraphStore(tmp_path / "task-graph.sqlite3")


async def test_mission_and_task_round_trip_and_revision(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task("root"))
    assert (await store.get_mission(MissionId("mission-1"))).objective == "Assess authorization"
    assert (await store.get_task(TaskId("root"))).title == "root"
    await store.update_mission(mission(2))
    assert (await store.get_mission(MissionId("mission-1"))).revision == 2


async def test_task_lifecycle_block_unblock_priority_and_invalid_transition(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task("root"))
    ready = await store.transition_task(TaskId("root"), TaskStatus.READY, now=NOW)
    assert ready.status is TaskStatus.READY
    blocked = await store.block_task(
        TaskId("root"), "operator pause", now=NOW + timedelta(seconds=1)
    )
    assert blocked.blocking_reason == "operator pause"
    assert (
        await store.unblock_task(TaskId("root"), now=NOW + timedelta(seconds=2))
    ).status is TaskStatus.READY
    assert (await store.update_priority(TaskId("root"), 50, now=NOW)).priority == 50
    await store.transition_task(TaskId("root"), TaskStatus.RUNNING, now=NOW)
    await store.transition_task(TaskId("root"), TaskStatus.COMPLETED, now=NOW)
    with pytest.raises(InvalidTaskTransition):
        await store.transition_task(TaskId("root"), TaskStatus.RUNNING, now=NOW)


async def test_dependencies_cycles_and_dynamic_split(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task("parent"))
    await store.create_task(task("dependent", dependencies=["parent"]))
    with pytest.raises(InvalidDependency):
        await store.transition_task(TaskId("dependent"), TaskStatus.READY, now=NOW)
    with pytest.raises(InvalidDependency):
        await store.add_dependency(TaskId("parent"), TaskId("dependent"), now=NOW)
    children = [task("child-a", parent="parent"), task("child-b", parent="parent")]
    assert len(await store.split_task(TaskId("parent"), children)) == 2
    assert {str(v.task_id) for v in await store.list_tasks(mission_id=MissionId("mission-1"))} == {
        "parent",
        "dependent",
        "child-a",
        "child-b",
    }


async def test_dependency_unblocks_only_after_completion(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task("first"))
    await store.create_task(task("next", status=TaskStatus.BLOCKED, dependencies=["first"]))
    assert (await store.unblock_task(TaskId("next"), now=NOW)).status is TaskStatus.PENDING
    await store.transition_task(TaskId("first"), TaskStatus.READY, now=NOW)
    await store.transition_task(TaskId("first"), TaskStatus.RUNNING, now=NOW)
    await store.transition_task(TaskId("first"), TaskStatus.COMPLETED, now=NOW)
    await store.block_task(TaskId("next"), "reevaluate", now=NOW)
    assert (await store.unblock_task(TaskId("next"), now=NOW)).status is TaskStatus.READY


async def test_sqlite_restart_recovery(tmp_path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = SQLiteTaskGraphStore(path)
    await first.create_mission(mission())
    await first.create_task(task("persisted", priority=9))
    second = SQLiteTaskGraphStore(path)
    assert (await second.get_mission(MissionId("mission-1"))).status is MissionStatus.ACTIVE
    assert [str(v.task_id) for v in await second.list_tasks()] == ["persisted"]


def test_contract_validation_intent_and_secret_boundaries() -> None:
    with pytest.raises(ValidationError):
        mission().model_copy(update={"objective": ""}).model_validate(
            mission().model_dump() | {"objective": ""}
        )
    with pytest.raises(ValidationError):
        MissionPlan.model_validate(
            mission().model_dump() | {"operator_constraints": {"password": "hunter2"}}
        )
    event = EventEnvelope(
        event_id=EventId("e"),
        event_type=EventType.MISSION_CREATED,
        run_id=RunId("r"),
        engagement_id=EngagementId("eng-1"),
        source="test",
        timestamp=NOW,
        payload={"token": "raw"},
    )
    assert event.payload["token"] == "***REDACTED***"
    assert "raw" not in event.model_dump_json()
    intent = OperatorIntent(
        intent_id=OperatorIntentId("intent-1"),
        mission_id=MissionId("mission-1"),
        intent_type=OperatorIntentType.REPRIORITIZE,
        instruction="focus authorization",
        task_ids=[TaskId("root")],
        parameters={"priority": 10},
        provenance=Provenance(source="operator"),
        created_at=NOW,
    )
    assert intent.intent_type is OperatorIntentType.REPRIORITIZE
    with pytest.raises(ValidationError):
        OperatorIntent.model_validate(intent.model_dump() | {"parameters": {"tool_id": "scanner"}})


def test_legacy_runtime_checkpoint_shape_and_engagement_yaml_still_load() -> None:
    legacy = {
        "run_id": "run-old",
        "engagement_id": "eng-old",
        "phase": "observe",
        "current_goal": "legacy",
    }
    restored = RuntimeState.model_validate(legacy)
    assert (
        restored.phase is AgentPhase.OBSERVE
        and restored.mission_id is None
        and restored.active_task_id is None
    )
    example = Path(__file__).resolve().parents[2] / "examples" / "engagement.yaml"
    assert load_engagement(example).name
