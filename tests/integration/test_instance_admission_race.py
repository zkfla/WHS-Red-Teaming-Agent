"""Admission race on the engine's atomic claim path (no engine edits here).

Two instances race the SAME unowned RUNNING task through
``claim_action_admission`` (which atomically adopts via ``claim_mission_task``
inside ``coordinator.change``): exactly one admission succeeds, the loser is
rejected before any provider call, and the rollback leaks neither claim nor
admission record. Runs on both memory and SQLite backends.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    MissionId,
    RunId,
    TaskId,
    ToolId,
)
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.engine import claim_action_admission, validate_current_action
from redteam.orchestration.paths import InMemoryPathStore, SQLitePathStore
from redteam.orchestration.semantic_completion import purpose_hash
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = "run-admit"
ENG = "eng-admit"
MISSION = "mission-admit"
TASK = "task-race"
OBJECTIVE = "perform task-race"


@pytest.fixture(params=["memory", "sqlite"])
def world(request, tmp_path):
    if request.param == "memory":
        tasks: Any = InMemoryTaskGraphStore()
        paths: Any = InMemoryPathStore()
    else:
        path = tmp_path / "admission.sqlite3"
        tasks = SQLiteTaskGraphStore(path)
        paths = SQLitePathStore(path)
    coordinator = coordinator_for(tasks, paths)
    deps = SimpleNamespace(
        coordinator=coordinator, task_graph_store=tasks, path_store=paths
    )
    return deps, coordinator


async def _seed(deps) -> None:
    mission = MissionPlan(
        mission_id=MissionId(MISSION),
        engagement_id=EngagementId(ENG),
        objective="Assess authorization",
        priorities=["authorization"],
        success_criteria=["evidence-backed result"],
        initial_workstreams=["API assessment"],
        assumptions=["test tenant available"],
        operator_constraints={"credential_ref": "vault://eng/tester"},
        operator_preferences={"focus": "API"},
        status=MissionStatus.ACTIVE,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="operator"),
    )
    await deps.task_graph_store.create_mission(mission)
    await deps.task_graph_store.create_task(
        MissionTask(
            task_id=TaskId(TASK),
            mission_id=MissionId(MISSION),
            title=TASK,
            objective=OBJECTIVE,
            status=TaskStatus.RUNNING,
            provenance=Provenance(source="planner"),
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _runtime(instance: str) -> RuntimeState:
    return RuntimeState(
        run_id=RunId(RUN),
        engagement_id=EngagementId(ENG),
        mission_id=MissionId(MISSION),
        active_task_id=TaskId(TASK),
        instance_id=AgentInstanceId(instance),
        current_goal="map the in-scope host",
        remaining_budget=10,
    )


def _step(instance: str) -> PlanStep:
    return PlanStep(
        step_id=f"step-{instance}",
        tool_id=ToolId("test.inventory"),
        reason="probe",
        binding_mode="selected_tool",
        execution_origin_version=1,
        mission_task_id=TASK,
        task_purpose_hash=purpose_hash(OBJECTIVE),
        model_tool_call_id=f"call-{instance}",
        operator_intent_id=None,
        capability="recon",
        operation="service_discovery",
        arguments={"host": "a.local"},
    )


def _proposal(instance: str) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(f"action-{instance}"),
        run_id=RunId(RUN),
        engagement_id=EngagementId(ENG),
        agent_id=AgentId("recon"),
        instance_id=AgentInstanceId(instance),
        binding_mode="selected_tool",
        tool_id=ToolId("test.inventory"),
        capability="recon",
        operation="service_discovery",
        target=None,
        reason="probe",
        created_at=NOW,
    )


async def _admission_records(coordinator, *action_ids: str) -> dict[str, Any]:
    async def read(change):
        return {
            action_id: change.records.get(("admission", action_id))
            for action_id in action_ids
        }

    return await coordinator.change(read)


async def test_same_unowned_running_task_admits_exactly_one(world) -> None:
    deps, coordinator = world
    await _seed(deps)
    assert (await deps.task_graph_store.get_task(TaskId(TASK))).occupant_owner is None
    await coordinator.bind(RUN, MISSION, ENG)
    invocations: list[str] = []

    async def admit_then_invoke(instance: str):
        # Mirrors execute_node's order: the provider call happens only
        # AFTER the atomic admission commits.
        runtime = _runtime(instance)
        await claim_action_admission(deps, runtime, _step(instance), _proposal(instance))
        invocations.append(instance)

    results = await asyncio.gather(
        admit_then_invoke("a"), admit_then_invoke("b"), return_exceptions=True
    )
    winners = [
        instance for instance, result in zip(("a", "b"), results, strict=True) if result is None
    ]
    losers = [
        (instance, result)
        for instance, result in zip(("a", "b"), results, strict=True)
        if result is not None
    ]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0][1], StateConflict)
    (winner,) = winners
    ((loser, _error),) = losers
    # The loser is rejected BEFORE any provider call for it.
    assert invocations == [winner]
    # The rollback leaks neither claim nor admission record.
    stored = await deps.task_graph_store.get_task(TaskId(TASK))
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == f"{RUN}:instance:{winner}"
    records = await _admission_records(coordinator, "action-a", "action-b")
    assert records[f"action-{winner}"]["mission_task_id"] == TASK
    assert records[f"action-{winner}"]["model_tool_call_id"] == f"call-{winner}"
    assert records[f"action-{loser}"] is None
    # The engine's pre-invocation guard rejects the foreign owner too.
    problems = await validate_current_action(deps, _runtime(loser), _step(loser))
    assert problems == ["native call no longer belongs to the current task purpose"]
