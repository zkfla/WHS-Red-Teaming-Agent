"""Claim cleanup is not permission to transfer an unfinished actor (synthetic)."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.core.ids import AgentInstanceId, EngagementId, MissionId, RunId, TaskId
from redteam.orchestration.coordination import CoordinationError, StateConflict
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.work.models import WorkStatus, work_owner_for
from tests.integration.test_agent_loop import _engagement, _loop_deps
from tests.unit.test_orchestration_coordination import ENG, MISSION, RUN, world
from tests.unit.test_work_execution_reconcile import _item


async def claim_world(tmp_path, backend, status=TaskStatus.RUNNING):
    w = await world(tmp_path, backend)
    runtime = RuntimeState(
        run_id=RunId(RUN), engagement_id=EngagementId(ENG), mission_id=MissionId(MISSION),
        instance_id=AgentInstanceId("original"), active_task_id=TaskId("task-claim"),
        phase=AgentPhase.FINISHED,
    )
    task = await w.tasks.get_task(TaskId("task-a"))
    await w.tasks.create_task(task.model_copy(update={
        "task_id": runtime.active_task_id,
        "occupant_owner": work_owner_for(runtime.run_id, runtime.instance_id), "status": status,
        "blocking_reason": "awaiting proof" if status is TaskStatus.BLOCKED else None,
    }))
    deps = replace(
        _loop_deps(_engagement()), task_graph_store=w.tasks, path_store=w.paths,
        coordinator=w.coordinator,
    )
    driver = InstanceDriver(deps, settings=OrchestrationSettings(checkpointing=False))
    return w, driver, runtime, deps


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("status", [
    TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.BLOCKED,
])
async def test_terminal_caller_cannot_release_unfinished_canonical_task(tmp_path, backend, status):
    w, driver, runtime, _deps = await claim_world(tmp_path, backend, status)
    before = await w.tasks.get_task(runtime.active_task_id)
    async with driver:
        with pytest.raises(StateConflict, match="task_claim_handoff_unverified"):
            await driver.release_instance_claim(runtime)
    assert await w.tasks.get_task(runtime.active_task_id) == before


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_terminal_cleanup_is_idempotent_and_preserves_effects_and_accounting(
    tmp_path, backend, status,
):
    w, driver, runtime, deps = await claim_world(tmp_path, backend, status)
    # Terminal Task status is NOT proof that effects/usage are known. Even
    # when cleaning its occupant, preserve independent Work and request holds.
    item = _item("pending-effect", run_id=runtime.run_id, instance_id=runtime.instance_id)
    item = item.model_copy(update={
        "status": WorkStatus.RUNNING, "mission_task_id": str(runtime.active_task_id),
        "resource_keys": ["resource:test"], "conflict_keys": ["resource:test"],
        "resource_execution_pending": True,
    })
    await deps.work_store.add(item)
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    request = await ledger.reserve(RUN, owner=runtime.work_owner, kind="native")
    await ledger.dispatch(RUN, request)
    before = await ledger.snapshot(RUN)
    task_before = await w.tasks.get_task(runtime.active_task_id)
    unrelated = await w.tasks.get_task(TaskId("task-b"))
    async with driver:
        await driver.release_instance_claim(runtime)
        first = await w.tasks.get_task(runtime.active_task_id)
        await driver.release_instance_claim(runtime)
    assert first.occupant_owner is None and first.status is status
    assert first.model_dump(exclude={"occupant_owner", "updated_at"}) == task_before.model_dump(
        exclude={"occupant_owner", "updated_at"},
    )
    assert await w.tasks.get_task(runtime.active_task_id) == first
    assert await w.tasks.get_task(TaskId("task-b")) == unrelated
    assert await deps.work_store.get(item.work_id) == item
    assert await ledger.snapshot(RUN) == before
    assert not deps.tool_gateway.invocations


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("case", ["mission", "engagement", "task", "instance", "missing_instance"])
async def test_cleanup_cannot_release_another_identity(tmp_path, backend, case):
    w, driver, runtime, _deps = await claim_world(tmp_path, backend, TaskStatus.CANCELLED)
    before = await w.tasks.get_task(runtime.active_task_id)
    fields = {
        "mission": {"mission_id": MissionId("other")},
        "engagement": {"engagement_id": EngagementId("other")},
        "task": {"active_task_id": TaskId("missing")},
        "instance": {"instance_id": AgentInstanceId("other")},
        "missing_instance": {"instance_id": None},
    }
    async with driver:
        with pytest.raises((CoordinationError, StateConflict)):
            await driver.release_instance_claim(runtime.model_copy(update=fields[case]))
    assert await w.tasks.get_task(runtime.active_task_id) == before


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_closed_run_cleanup_does_not_reopen_binding_or_cancelled_task(tmp_path, backend):
    w, driver, runtime, _deps = await claim_world(tmp_path, backend, TaskStatus.CANCELLED)
    async def close(tx):
        tx.binding(RUN, MISSION, ENG)["closed"] = True
    await w.coordinator.change(close)
    async with driver:
        await driver.release_instance_claim(runtime)
    async def read(tx):
        return tx.binding(RUN, MISSION, ENG)["closed"]
    assert await w.coordinator.change(read) is True
    task = await w.tasks.get_task(runtime.active_task_id)
    assert task.status is TaskStatus.CANCELLED and task.occupant_owner is None


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_other_driver_ownership_refuses_cleanup(tmp_path, backend):
    w, driver, runtime, deps = await claim_world(tmp_path, backend, TaskStatus.CANCELLED)
    before = await w.tasks.get_task(runtime.active_task_id)
    async with InstanceDriver(deps) as other, driver:
        await other.own_run(RUN)
        with pytest.raises(CoordinationError, match="run_execution_already_owned"):
            await driver.release_instance_claim(runtime)
    assert await w.tasks.get_task(runtime.active_task_id) == before


async def test_live_actor_refuses_cleanup_before_store_mutation(tmp_path, monkeypatch):
    w, driver, runtime, _deps = await claim_world(tmp_path, "memory", TaskStatus.CANCELLED)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def run(base):
        entered.set()
        await finish.wait()
        return base
    async with driver:
        monkeypatch.setattr(driver.runner_for(runtime), "run", AsyncMock(side_effect=run))
        iterator = driver.drive([runtime])
        head = asyncio.create_task(iterator.__anext__())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(StateConflict, match="requires_quiescent_driver"):
                await driver.release_instance_claim(runtime)
            task = await w.tasks.get_task(runtime.active_task_id)
            assert task.occupant_owner == runtime.work_owner
        finally:
            finish.set()
            await asyncio.wait_for(head, 5)
            await iterator.aclose()


async def test_closed_driver_cannot_clean_claim(tmp_path):
    w, driver, runtime, _deps = await claim_world(tmp_path, "memory", TaskStatus.CANCELLED)
    before = await w.tasks.get_task(runtime.active_task_id)
    await driver.open()
    await driver.close()
    with pytest.raises(RuntimeError, match="not open"):
        await driver.release_instance_claim(runtime)
    assert await w.tasks.get_task(runtime.active_task_id) == before


async def test_cancellation_before_transaction_keeps_claim_and_cleans_driver(tmp_path, monkeypatch):
    w, driver, runtime, _deps = await claim_world(tmp_path, "memory", TaskStatus.CANCELLED)
    before = await w.tasks.get_task(runtime.active_task_id)
    entered = asyncio.Event()
    original_change = w.coordinator.change
    async def paused_change(callback):
        entered.set()
        await asyncio.Event().wait()
        return await original_change(callback)
    async with driver:
        monkeypatch.setattr(w.coordinator, "change", paused_change)
        job = asyncio.create_task(driver.release_instance_claim(runtime))
        await asyncio.wait_for(entered.wait(), 5)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        monkeypatch.setattr(w.coordinator, "change", original_change)
    assert await w.tasks.get_task(runtime.active_task_id) == before
