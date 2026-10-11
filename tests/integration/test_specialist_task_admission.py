"""Atomization writes only canonical Tasks/outbox, with relevant-input CAS."""

import asyncio

import pytest

from redteam.core.contracts.completion import completion_contract_hash
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.specialist_tasks import StrategyTasks, register_strategy_tasks
from tests.unit.test_orchestration_coordination import MISSION, NOW, RUN, world


def proposal():
    condition = {
        "kind": "semantic", "description": "Observe the declared response", "min_evidence": 1,
    }
    question = {
        "question_id": "q", "objective": "Observe authorized host",
        "target_node_ids": ["host:unit.local"], "completion_criteria": [condition],
    }
    return StrategyTasks.model_validate({
        "questions": [question],
        "tasks": [{
            "task_key": "t", "question_id": "q", "objective": question["objective"],
            "title": "Observe host", "role_id": "web-api-agent",
            "target_node_ids": question["target_node_ids"], "completion_criteria": [condition],
        }],
    })


async def prepared(tmp_path, backend="memory"):
    w = await world(tmp_path, backend)
    service = PlanningAdmission(coordinator_for(w.tasks, w.paths))
    await service.bind_inputs(RUN, {"scope": "synthetic", "profile": "test-v1"})
    stamp = await service.capture(RUN, MISSION, task_ids=["task-a"], reference_keys=["evidence:x"])
    return w, service, stamp


async def register(service, stamp, *, identity="proposal-a", candidate=None):
    return await register_strategy_tasks(
        service, stamp, proposal_id=identity, proposal=candidate or proposal(),
        agents=default_registry(), known_targets={"host:unit.local"}, known_principals=set(),
        max_tasks=8, now=NOW,
    )


async def outbox_count(service):
    async def count(tx):
        return sum(kind == "outbox" for kind, _ in tx.records)
    return await service.coordinator.change(count)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_canonical_tasks_conditions_outbox_and_idempotency(tmp_path, backend):
    w, service, stamp = await prepared(tmp_path, backend)
    first = await register(service, stamp)
    task = await w.tasks.get_task(first[0])
    assert task.completion_contract_version == 2
    assert task.completion_criteria == proposal().questions[0].completion_criteria
    assert completion_contract_hash(task)
    assert task.assigned_agent == "web-api-agent"
    assert task.provenance.metadata["question_id"] == "q"
    assert await outbox_count(service) == 1
    assert await register(service, stamp) == first
    assert len(await w.tasks.list_tasks(mission_id=MISSION)) == 3
    assert await outbox_count(service) == 1


async def test_late_old_goal_plan_cannot_overwrite_new_admitted_plan(tmp_path):
    w, service, old = await prepared(tmp_path)
    mission = await w.tasks.get_mission(MISSION)
    await w.tasks.update_mission(mission.model_copy(update={
        "objective": "New user purpose", "revision": mission.revision + 1,
    }))
    new = await service.capture(RUN, MISSION)
    await register(service, new, identity="proposal-new")
    with pytest.raises(StateConflict, match="stale_plan"):
        await register(service, old)
    assert len(await w.tasks.list_tasks(mission_id=MISSION)) == 3
    assert await outbox_count(service) == 1


async def test_competing_plans_only_one_wins(tmp_path):
    w, service, stamp = await prepared(tmp_path)
    results = await asyncio.gather(
        register(service, stamp, identity="a"), register(service, stamp, identity="b"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, StateConflict) for item in results) == 1
    assert len(await w.tasks.list_tasks(mission_id=MISSION)) == 3
    assert await outbox_count(service) == 1


async def test_same_id_different_content_is_not_a_replay(tmp_path):
    _, service, stamp = await prepared(tmp_path)
    await register(service, stamp)
    altered = proposal()
    altered.tasks[0].title = "Different content"
    with pytest.raises(StateConflict, match="identity_conflict"):
        await register(service, stamp, candidate=altered)


async def test_unrelated_sibling_and_bare_revision_do_not_invalidate(tmp_path):
    w, service, stamp = await prepared(tmp_path)
    task = await w.tasks.get_task("task-b")
    await w.tasks.update_task(task.model_copy(update={"objective": "Sibling changed"}))
    mission = await w.tasks.get_mission(MISSION)
    await w.tasks.update_mission(mission.model_copy(update={"revision": mission.revision + 1}))
    assert await register(service, stamp)


async def test_relevant_task_contract_change_invalidates(tmp_path):
    w, service, stamp = await prepared(tmp_path)
    task = await w.tasks.get_task("task-a")
    await w.tasks.update_task(task.model_copy(update={"objective": "Changed relevant input"}))
    with pytest.raises(StateConflict, match="task_changed"):
        await register(service, stamp)
    assert await outbox_count(service) == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_mutable_write_fence_blocks_final_admission(tmp_path, backend):
    _, service, stamp = await prepared(tmp_path, backend)
    # External write begins after model snapshot but before task commit.
    writer = await service.begin_reference_update(RUN, ["evidence:x"])
    with pytest.raises(StateConflict, match="reference_changed"):
        await register(service, stamp)
    with pytest.raises(StateConflict, match="update_pending"):
        await service.capture(RUN, MISSION, reference_keys=["evidence:x"])
    assert await outbox_count(service) == 0
    await service.finish_reference_update(RUN, ["evidence:x"], writer)
    with pytest.raises(StateConflict, match="reference_changed"):
        await register(service, stamp)
    fresh = await service.capture(RUN, MISSION, reference_keys=["evidence:x"])
    assert await register(service, fresh)


async def test_unrelated_reference_write_does_not_block(tmp_path):
    _, service, stamp = await prepared(tmp_path)
    await service.begin_reference_update(RUN, ["evidence:unrelated"])
    assert await register(service, stamp)


@pytest.mark.parametrize("failure", ["criteria", "role", "cycle", "target", "principal"])
async def test_invalid_decomposition_has_no_partial_effects(tmp_path, failure):
    w, service, stamp = await prepared(tmp_path)
    candidate = proposal()
    child = candidate.tasks[0]
    if failure == "criteria":
        child.completion_criteria[0].min_evidence = 2
    elif failure == "role":
        child.role_id = "not-registered"
    elif failure == "cycle":
        child.depends_on = [child.task_key]
    elif failure == "target":
        child.target_node_ids = ["host:unknown.local"]
    else:
        child.principal_refs = ["invented-account"]
    with pytest.raises(ValueError):
        await register(service, stamp, candidate=candidate)
    assert len(await w.tasks.list_tasks(mission_id=MISSION)) == 2
    assert await outbox_count(service) == 0


async def test_atomic_failure_rolls_back_tasks_revision_and_proposal(tmp_path):
    w, service, stamp = await prepared(tmp_path, "sqlite")

    async def fail(tx):
        task = await tx.tasks.get_task("task-a")
        await tx.tasks.update_task(task.model_copy(update={"objective": "Must roll back"}))
        raise RuntimeError("synthetic persistence failure")

    with pytest.raises(RuntimeError):
        await service.admit(stamp, proposal_id="p", proposal={}, apply=fail)
    assert (await w.tasks.get_task("task-a")).objective != "Must roll back"
    # Same base still usable: no revision/applied record leaked.
    assert await register(service, stamp)
