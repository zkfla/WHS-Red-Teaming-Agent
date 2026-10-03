"""Stored Mission state and run ownership take precedence over old checkpoints."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.orchestration.component_binding import builtin_component_binding
from redteam.orchestration.coordination import CoordinationError, coordinator_for
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.state import RuntimeState, TerminationReason
from tests.fakes import build_test_application

RUN, MISSION, ENG = "run-owner", "mission-owner", "eng-owner"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


async def setup(status, reason):
    deps = build_test_application()
    mission = MissionPlan(
        mission_id=MISSION,
        engagement_id=ENG,
        objective="Review authorized target",
        status=status,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )
    await deps.task_graph_store.create_mission(mission)
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    await coordinator.bind(RUN, MISSION, ENG)
    runtime = RuntimeState(
        run_id=RUN,
        mission_id=MISSION,
        engagement_id=ENG,
        phase=AgentPhase.FINISHED,
        termination_reason=reason,
        step_count=4,
    )

    class Graph:
        calls = 0

        async def aget_state(self, config):
            return SimpleNamespace(values={"runtime": runtime}, next=[])

        async def ainvoke(self, state, config):
            self.calls += 1
            return state

    runner = AgentLoopRunner(deps)
    graph = Graph()
    runner._graph = graph
    return deps, runner, graph, runtime, coordinator


@pytest.mark.parametrize(
    "status",
    [
        MissionStatus.COMPLETED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
    ],
)
async def test_old_quiescent_checkpoint_cannot_revive_settled_mission(status):
    deps, runner, graph, runtime, _ = await setup(status, TerminationReason.NO_ACTIONABLE_PLAN)
    before = await deps.task_graph_store.get_mission(MISSION)
    assert await runner.resume_recovered(runtime) is runtime
    reconciled = await runner.reconcile_once(runtime, timeout_seconds=0)
    assert not reconciled.resumed
    assert graph.calls == 0
    assert await deps.task_graph_store.get_mission(MISSION) == before


@pytest.mark.parametrize(
    "status",
    [
        MissionStatus.COMPLETED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
        MissionStatus.PAUSED,
    ],
)
async def test_direct_run_refuses_inactive_latest_mission(status):
    deps, runner, graph, runtime, _ = await setup(status, None)
    incoming = runtime.model_copy(update={"phase": AgentPhase.OBSERVE, "step_count": 0})
    with pytest.raises(CoordinationError):
        await runner.run(incoming)
    assert graph.calls == 0
    assert (await deps.task_graph_store.get_mission(MISSION)).status is status


@pytest.mark.parametrize(
    "reason",
    [
        TerminationReason.MAX_STEPS_REACHED,
        TerminationReason.BUDGET_EXHAUSTED,
        TerminationReason.PLANNING_ERROR,
        TerminationReason.EXECUTION_ERROR,
        TerminationReason.OPERATOR_STOPPED,
        TerminationReason.USER_INTERRUPTED,
    ],
)
async def test_blocked_mission_hard_stop_never_reactivates(reason):
    deps, runner, graph, runtime, _ = await setup(MissionStatus.BLOCKED, reason)
    before = await deps.task_graph_store.get_mission(MISSION)
    assert await runner.resume_recovered(runtime) is runtime
    assert graph.calls == 0
    assert await deps.task_graph_store.get_mission(MISSION) == before


async def test_closed_run_binding_cannot_revive_active_mission():
    deps, runner, graph, runtime, coordinator = await setup(
        MissionStatus.ACTIVE,
        TerminationReason.NO_ACTIONABLE_PLAN,
    )

    async def close(change):
        change.binding(RUN, MISSION)["closed"] = True

    await coordinator.change(close)
    assert await runner.resume_recovered(runtime) is runtime
    assert graph.calls == 0
    assert (await deps.task_graph_store.get_mission(MISSION)).status is MissionStatus.ACTIVE


async def test_foreign_engagement_checkpoint_cannot_use_existing_run_binding():
    _, runner, graph, runtime, _ = await setup(
        MissionStatus.ACTIVE,
        TerminationReason.NO_ACTIONABLE_PLAN,
    )
    foreign = runtime.model_copy(update={"engagement_id": "foreign-engagement"})
    assert await runner.resume_recovered(foreign) is foreign
    assert graph.calls == 0


@pytest.mark.parametrize("legacy", [False, True])
async def test_resume_refuses_a_different_component_assembly(legacy):
    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.ACTIVE, TerminationReason.NO_ACTIONABLE_PLAN
    )
    selected = builtin_component_binding().model_copy(update={"profile_id": "custom"})
    object.__setattr__(deps, "component_binding", selected)
    if not legacy:
        runtime.component_binding = builtin_component_binding()
    assert await runner.resume_recovered(runtime) is runtime
    assert graph.calls == 0


async def test_resume_refuses_changed_checkpoint_component_selection():
    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.ACTIVE, TerminationReason.NO_ACTIONABLE_PLAN
    )
    selected = builtin_component_binding()
    object.__setattr__(deps, "component_binding", selected)
    runtime.component_binding = selected.model_copy(update={"profile_id": "old"})
    incoming = runtime.model_copy(update={"component_binding": selected})
    assert await runner.resume_recovered(incoming) is incoming
    assert graph.calls == 0


async def test_recorded_builtin_cannot_resume_with_unidentified_components():
    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.ACTIVE, TerminationReason.INCOMPLETE_ACTIVE_WORK
    )
    assert deps.component_binding is None
    runtime.component_binding = builtin_component_binding()
    assert await runner.resume_recovered(runtime) is runtime
    assert graph.calls == 0


async def test_recorded_builtin_resumes_with_matching_registered_components():
    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.BLOCKED, TerminationReason.INCOMPLETE_ACTIVE_WORK
    )
    binding = builtin_component_binding()
    object.__setattr__(deps, "component_binding", binding)
    runtime.component_binding = binding
    await runner.resume_recovered(runtime)
    assert graph.calls == 1


async def test_reconcilable_blocked_mission_reactivates_atomically_before_graph():
    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.BLOCKED,
        TerminationReason.INCOMPLETE_ACTIVE_WORK,
    )
    original = await deps.task_graph_store.get_mission(MISSION)
    await runner.resume_recovered(runtime)
    current = await deps.task_graph_store.get_mission(MISSION)
    assert graph.calls == 1
    assert current.status is MissionStatus.ACTIVE
    assert current.revision == original.revision + 1


async def test_pending_operator_change_prevents_blocked_reactivation():
    from redteam.orchestration.operator_intents import (
        InMemoryOperatorIntentStore,
        OperatorIntentService,
    )

    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.ACTIVE,
        TerminationReason.INCOMPLETE_ACTIVE_WORK,
    )
    service = OperatorIntentService(
        store=InMemoryOperatorIntentStore(),
        task_graph_store=deps.task_graph_store,
        path_store=deps.path_store,
        event_bus=deps.event_bus,
    )
    received = await service.receive("focus web", mission_id=MISSION, run_id=RUN)
    assert received.intent_id is not None
    mission = await deps.task_graph_store.get_mission(MISSION)
    blocked = mission.model_copy(
        update={
            "status": MissionStatus.BLOCKED,
            "revision": mission.revision + 1,
        }
    )
    await deps.task_graph_store.update_mission(blocked)
    assert await runner.resume_recovered(runtime) is runtime
    assert graph.calls == 0
    assert await deps.task_graph_store.get_mission(MISSION) == blocked


@pytest.mark.parametrize("change", ["reason", "task", "unsafe_checkpoint"])
async def test_registered_binding_still_requires_original_safe_checkpoint(change):
    deps, runner, graph, runtime, _ = await setup(
        MissionStatus.BLOCKED, TerminationReason.NO_ACTIONABLE_PLAN
    )
    original = await deps.task_graph_store.get_mission(MISSION)
    if change == "reason":
        incoming = runtime.model_copy(
            update={"termination_reason": TerminationReason.INCOMPLETE_ACTIVE_WORK}
        )
    elif change == "task":
        incoming = runtime.model_copy(update={"active_task_id": "foreign-task"})
    else:
        incoming = runtime

        async def unsafe(config):
            return SimpleNamespace(values={"runtime": runtime}, next=["execute"])

        graph.aget_state = unsafe
    assert await runner.resume_recovered(incoming) is incoming
    assert graph.calls == 0
    assert await deps.task_graph_store.get_mission(MISSION) == original


async def test_omitting_mission_cannot_bypass_registered_closed_run():
    _, runner, graph, runtime, _ = await setup(MissionStatus.COMPLETED, None)
    stripped = runtime.model_copy(
        update={"mission_id": None, "phase": AgentPhase.OBSERVE, "step_count": 0}
    )
    with pytest.raises(CoordinationError):
        await runner.run(stripped)
    assert graph.calls == 0


@pytest.mark.parametrize(
    "reason",
    [TerminationReason.MAX_STEPS_REACHED, TerminationReason.PLANNING_ERROR],
)
async def test_hard_stop_closes_run_before_app_settlement(reason):
    deps, runner, graph, runtime, _ = await setup(MissionStatus.ACTIVE, reason)
    incoming = runtime.model_copy(
        update={"phase": AgentPhase.OBSERVE, "step_count": 0, "termination_reason": None}
    )

    async def terminate(state, config):
        graph.calls += 1
        return {**state, "runtime": runtime}

    graph.ainvoke = terminate
    assert await runner.run(incoming) == runtime
    assert (await deps.task_graph_store.get_mission(MISSION)).status is MissionStatus.ACTIVE
    with pytest.raises(CoordinationError):
        await runner.run(incoming)
    assert graph.calls == 1

    from redteam.orchestration.operator_intents import (
        InMemoryOperatorIntentStore,
        OperatorIntentService,
    )

    service = OperatorIntentService(
        store=InMemoryOperatorIntentStore(),
        task_graph_store=deps.task_graph_store,
        path_store=deps.path_store,
        event_bus=deps.event_bus,
    )
    rejected = await service.receive_and_apply(
        "focus web", mission_id=MISSION, run_id=RUN
    )
    assert not rejected.applied
    assert rejected.reason == "mission_closed"
