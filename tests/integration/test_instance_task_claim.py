"""Observe-level instance task claims: no duplicate occupancy.

Two instances sharing one run/mission claim through ONE ``coordinator.change``
each: the second instance never takes the first instance's task. Role
assignment (``assigned_agent``) is respected and never confused with the
instance owner; legacy unowned RUNNING rows are adopted, not duplicated.
"""

from __future__ import annotations

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import AgentInstanceId, EngagementId, MissionId, RunId, TaskId
from redteam.orchestration.engine import InvestigationEngine, TurnDecision
from redteam.orchestration.nodes.observe import observe_node
from redteam.orchestration.paths import PathPlan
from redteam.orchestration.state import RuntimeState, initial_graph_state
from tests.integration import _engine_llm_harness as h


async def _world_with_tasks(*tasks):
    seed = await h.seed_host("run-claim")
    world = h.make_world(h.ScriptedLLM(), graph_store=seed.store)
    mission, _ = h.make_mission_task(
        mission_id="mission-claim", engagement_id="eng-claim", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    await world.tasks.create_mission(mission)
    for task in tasks:
        await world.tasks.create_task(task)
    return world


def _task(task_id: str, base) -> object:
    return base.model_copy(update={"task_id": TaskId(task_id), "title": task_id})


def _runtime(instance: str) -> RuntimeState:
    return RuntimeState(
        run_id=RunId("run-claim"),
        engagement_id=EngagementId("eng-claim"),
        mission_id=MissionId("mission-claim"),
        instance_id=AgentInstanceId(instance),
        current_goal="map the in-scope host",
        remaining_budget=10,
    )


async def _observe(world, runtime: RuntimeState):
    return await observe_node(initial_graph_state(runtime), deps=world.deps)


async def test_two_instances_claim_distinct_tasks() -> None:
    _, template = h.make_mission_task(
        mission_id="mission-claim", engagement_id="eng-claim", criteria=["actual response"]
    )
    world = await _world_with_tasks(_task("task-t1", template), _task("task-t2", template))
    out_a = await _observe(world, _runtime("a"))
    out_b = await _observe(world, _runtime("b"))
    id_a = out_a["runtime"].active_task_id
    id_b = out_b["runtime"].active_task_id
    assert id_a is not None and id_b is not None
    assert str(id_a) != str(id_b)
    assert {str(id_a), str(id_b)} == {"task-t1", "task-t2"}
    occ_a = (await world.tasks.get_task(id_a)).occupant_owner
    occ_b = (await world.tasks.get_task(id_b)).occupant_owner
    assert occ_a == "run-claim:instance:a"
    assert occ_b == "run-claim:instance:b"


async def test_single_ready_task_second_instance_gets_none() -> None:
    _, template = h.make_mission_task(
        mission_id="mission-claim", engagement_id="eng-claim", criteria=["actual response"]
    )
    world = await _world_with_tasks(_task("task-only", template))
    out_a = await _observe(world, _runtime("a"))
    assert str(out_a["runtime"].active_task_id) == "task-only"
    out_b = await _observe(world, _runtime("b"))
    assert out_b["runtime"].active_task_id is None
    stored = await world.tasks.get_task(TaskId("task-only"))
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == "run-claim:instance:a"


async def test_role_assignment_respected_not_seized() -> None:
    _, template = h.make_mission_task(
        mission_id="mission-claim", engagement_id="eng-claim", criteria=["actual response"]
    )
    seed = await h.seed_host("run-claim")
    world = h.make_world(h.ScriptedLLM(), graph_store=seed.store)
    mission, _ = h.make_mission_task(
        mission_id="mission-claim", engagement_id="eng-claim", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    await world.tasks.create_mission(mission)
    registry_ids = [agent.agent_id for agent in world.deps.agents.all()]
    assert len(registry_ids) >= 2
    mine, other = registry_ids[0], registry_ids[1]
    await world.tasks.create_task(
        template.model_copy(update={"task_id": TaskId("task-mine"), "assigned_agent": mine})
    )
    await world.tasks.create_task(
        template.model_copy(update={"task_id": TaskId("task-other"), "assigned_agent": other})
    )
    rt = _runtime("a").model_copy(update={"current_agent": other})
    out = await _observe(world, rt)
    # Never seizes the other role's task; takes its own.
    assert str(out["runtime"].active_task_id) == "task-other"
    rt2 = _runtime("b").model_copy(update={"current_agent": mine})
    out2 = await _observe(world, rt2)
    assert str(out2["runtime"].active_task_id) == "task-mine"


async def test_legacy_running_task_adopted_not_duplicated() -> None:
    _, template = h.make_mission_task(
        mission_id="mission-claim", engagement_id="eng-claim", criteria=["actual response"]
    )
    legacy = template.model_copy(
        update={"task_id": TaskId("task-legacy"), "status": TaskStatus.RUNNING}
    )
    world = await _world_with_tasks(legacy)
    assert (await world.tasks.get_task(TaskId("task-legacy"))).occupant_owner is None
    out = await _observe(world, _runtime("a"))
    assert str(out["runtime"].active_task_id) == "task-legacy"
    stored = await world.tasks.get_task(TaskId("task-legacy"))
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == "run-claim:instance:a"


class _ClaimReviewer:
    """Answer the task-completion claims review with a grounded accept."""

    async def complete_structured(self, **kwargs):
        return h.task_accept(["proof-claim"])


async def _completion_world(occupant: str | None):
    seed = await h.seed_host("run-settle")
    world = h.make_world(_ClaimReviewer(), graph_store=seed.store)
    mission, task = h.make_mission_task(
        mission_id="mission-settle", engagement_id="eng-settle", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    task.status = TaskStatus.RUNNING
    if occupant is not None:
        task = task.model_copy(update={"occupant_owner": occupant})
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-settle", "eng-settle", mission_id="mission-settle", task_id=str(task.task_id)
    )
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    raw = h.draft_path(seed.node_id)
    raw.update(
        path_id="path-settle",
        run_id=str(runtime.run_id),
        version=1,
        status="active",
        mission_id=str(mission.mission_id),
        mission_task_id=str(task.task_id),
        goal=task.objective,
    )
    raw["steps"][0].update(status="completed", outcome="success", evidence_ids=["proof-claim"])
    path = await world.paths.register(PathPlan.model_validate(raw))
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-claim",
            run_id=str(runtime.run_id),
            engagement_id="eng-settle",
            action_id="action-settle",
            invocation_id="inv-settle",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=seed.node_id,
        )
    )
    return world, runtime, path


async def test_completion_refused_for_other_owner() -> None:
    world, runtime, path = await _completion_world("run-settle:instance:zzz")
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    with pytest.raises(ValueError, match="completion_task_claimed_by_other"):
        await engine.complete_task(
            path,
            TurnDecision(mode="complete_task", reason="evaluate actual evidence"),
            runtime,
            context,
        )
    stored = await world.tasks.get_task(runtime.active_task_id)
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == "run-settle:instance:zzz"


async def test_completion_by_owner_clears_claim() -> None:
    world, runtime, path = await _completion_world("run-settle")
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    errors = await engine.complete_task(
        path,
        TurnDecision(mode="complete_task", reason="evaluate actual evidence"),
        runtime,
        context,
    )
    assert errors == []
    stored = await world.tasks.get_task(runtime.active_task_id)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.occupant_owner is None
    assert stored.completion_evidence_id is not None
