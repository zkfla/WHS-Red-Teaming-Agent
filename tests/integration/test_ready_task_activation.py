"""Atomic activation/driver scheduling; native regression uses synthetic tools."""

import asyncio

import pytest

from redteam.core.enums import MissionStatus, TaskStatus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_activation import TaskActivation
from tests.unit.test_orchestration_coordination import ENG, MISSION, RUN, world


async def prepared(tmp_path, backend="memory"):
    w = await world(tmp_path, backend)
    for task in await w.tasks.list_tasks(mission_id=MISSION):
        await w.tasks.update_task(task.model_copy(update={"assigned_agent": "web-api-agent"}))
    activation = TaskActivation(coordinator_for(w.tasks, w.paths), default_registry())
    base = RuntimeState(run_id=RUN, mission_id=MISSION, engagement_id=ENG, remaining_budget=9)
    return w, activation, base


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_atomic_launch_claim_and_duplicate_race(tmp_path, backend):
    w, activation, base = await prepared(tmp_path, backend)
    batches = await asyncio.gather(*(
        activation.ready(base, max_instances=2, max_activations=4) for _ in range(5)
    ))
    launched = [runtime for batch in batches for runtime in batch]
    assert len(launched) == 2
    assert len({r.instance_id for r in launched}) == 2
    assert len({r.started_at for r in launched}) == 1
    for runtime in launched:
        stored = await w.tasks.get_task(runtime.active_task_id)
        assert stored.occupant_owner == runtime.work_owner
        assert stored.status == TaskStatus.RUNNING
    assert await activation.ready(base, max_instances=2, max_activations=4) == []


@pytest.mark.parametrize("status", [MissionStatus.PAUSED, MissionStatus.CANCELLED])
async def test_stop_and_pause_do_not_spawn(tmp_path, status):
    w, activation, base = await prepared(tmp_path)
    mission = await w.tasks.get_mission(MISSION)
    await w.tasks.update_mission(mission.model_copy(update={
        "status": status, "revision": mission.revision + 1,
    }))
    assert await activation.ready(base, max_instances=2, max_activations=4) == []
    assert all(t.occupant_owner is None for t in await w.tasks.list_tasks(mission_id=MISSION))


async def test_held_approval_and_unknown_keep_original_identity_not_relaunched(tmp_path):
    w, activation, base = await prepared(tmp_path)
    launched = await activation.ready(base, max_instances=2, max_activations=4)
    await activation.returned(launched[0].model_copy(update={"pending_approval_id": "appr-unit"}))
    await activation.returned(launched[1], failed=True)
    assert await activation.ready(base, max_instances=2, max_activations=4) == []
    for runtime in launched:
        assert (await w.tasks.get_task(runtime.active_task_id)).occupant_owner == runtime.work_owner


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_launch_origin_survives_completion_context_clear_without_reclaim(tmp_path, backend):
    w, activation, base = await prepared(tmp_path, backend)
    assert await activation.source_task_id(base) is None
    launched = (await activation.ready(base, max_instances=1, max_activations=2))[0]
    returned = launched.model_copy(update={"active_task_id": None})
    await activation.returned(returned)
    recovered = TaskActivation(coordinator_for(w.tasks, w.paths), default_registry())
    assert await recovered.source_task_id(returned) == launched.active_task_id
    assert (await w.tasks.get_task(launched.active_task_id)).occupant_owner == launched.work_owner


async def test_exhausted_shared_request_pool_does_not_claim_or_launch(tmp_path):
    from redteam.core.contracts.llm_policy import LLMRequestPolicy
    from redteam.orchestration.llm_requests import RunRequestLedger

    w, activation, base = await prepared(tmp_path)
    ledger = RunRequestLedger(activation.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=1))
    request = await ledger.reserve(RUN, owner="planner", kind="structured")
    await ledger.dispatch(RUN, request)
    await ledger.settle(RUN, request)
    assert await activation.ready(base, max_instances=2, max_activations=4) == []
    assert all(t.occupant_owner is None for t in await w.tasks.list_tasks(mission_id=MISSION))


async def test_role_readiness_blocks_launch_without_changing_task(tmp_path):
    w, activation, base = await prepared(tmp_path)
    assert await activation.ready(
        base, max_instances=2, max_activations=4, eligible_roles=set(),
    ) == []
    assert all(t.status == TaskStatus.READY and t.occupant_owner is None
               for t in await w.tasks.list_tasks(mission_id=MISSION))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_resume_occupies_existing_slot_without_spending_another_activation(tmp_path, backend):
    w, activation, base = await prepared(tmp_path, backend)
    first = (await activation.ready(base, max_instances=1, max_activations=2))[0]
    await activation.returned(first.model_copy(update={"pending_approval_id": "appr-unit"}))
    await activation.resuming([first])
    assert await activation.ready(base, max_instances=1, max_activations=2) == []
    await activation.returned(first)
    second = (await activation.ready(base, max_instances=1, max_activations=2))[0]
    assert second.instance_id != first.instance_id
    assert (await w.tasks.get_task(first.active_task_id)).occupant_owner == first.work_owner


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_bad_resume_selection_changes_no_other_launch_record(tmp_path, backend):
    from redteam.orchestration.coordination import StateConflict

    _, activation, base = await prepared(tmp_path, backend)
    first, second = await activation.ready(base, max_instances=2, max_activations=2)
    await activation.returned(first, failed=True)
    await activation.returned(second, failed=True)
    with pytest.raises(StateConflict, match="role_mismatch"):
        await activation.resuming([first, second.model_copy(update={"current_agent": "bad"})])

    async def states(tx):
        return [row["state"] for (kind, _), row in tx.records.items()
                if kind == "task_activation"]

    assert await activation.coordinator.change(states) == ["held", "held"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("changed", ["contract", "claim"])
async def test_resume_refuses_changed_task_contract_or_owner(tmp_path, backend, changed):
    from redteam.orchestration.coordination import StateConflict

    w, activation, base = await prepared(tmp_path, backend)
    first = (await activation.ready(base, max_instances=1, max_activations=2))[0]
    await activation.returned(first, failed=True)
    task = await w.tasks.get_task(first.active_task_id)
    update = ({"objective": "Operator changed purpose"} if changed == "contract"
              else {"occupant_owner": "another-owner"})
    await w.tasks.update_task(task.model_copy(update=update))
    with pytest.raises(StateConflict, match=f"{changed}_changed"):
        await activation.resuming([first])
    current = await w.tasks.get_task(first.active_task_id)
    assert current.objective == update.get("objective", task.objective)
    assert current.occupant_owner == update.get("occupant_owner", task.occupant_owner)


async def test_exclusion_and_launch_limit_survive_repeated_wakeup(tmp_path):
    w, activation, base = await prepared(tmp_path)
    mission = await w.tasks.get_mission(MISSION)
    await w.tasks.update_mission(mission.model_copy(update={
        "operator_preferences": {"excluded_task_ids": ["task-a"]},
        "revision": mission.revision + 1,
    }))
    launched = await activation.ready(base, max_instances=2, max_activations=1)
    assert [str(r.active_task_id) for r in launched] == ["task-b"]
    await activation.returned(launched[0])
    template = await w.tasks.get_task("task-b")
    await w.tasks.create_task(template.model_copy(update={
        "task_id": "task-c", "status": TaskStatus.READY, "occupant_owner": None,
    }))
    assert await activation.ready(base, max_instances=2, max_activations=1) == []
