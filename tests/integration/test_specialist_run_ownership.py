"""Ownership at the public specialist start/cohort recovery boundaries.

Judgments/tools are synthetic. SQLite checkpoint/recovery and the application
entrypoints are real; fake actors isolate driver teardown lifetime only.
"""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from redteam.app.cli.commands.resume import _drive_cohort_guarded
from redteam.app.specialist_run import run_specialists
from redteam.orchestration.coordination import CoordinationError, coordinator_for
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.integration.test_native_specialist_closed_loop import ClosedLoopProvider, assembled
from tests.integration.test_specialist_resume import OneApprovalControl, identities, resume
from tests.unit.test_instance_driver import _FakeRunner, _runtime, _StubDriver


def independent_deps(deps):
    """Different coordinator/connection with exactly the same durable store."""
    tasks = SQLiteTaskGraphStore(deps.task_graph_store._path)
    paths = SQLitePathStore(deps.path_store._path)
    coordinator = coordinator_for(tasks, paths)
    client = RunMeteredLLM(deps.llm_client.client, RunRequestLedger(coordinator),
                          prompts=deps.llm_client.prompts)
    return replace(deps, task_graph_store=tasks, path_store=paths,
                   coordinator=coordinator, llm_client=client)


async def saved_records(deps):
    async def read(tx):
        return copy.deepcopy(tx.records)
    return await deps.coordinator.change(read)


@pytest.mark.parametrize("durable", [False, True])
async def test_fresh_specialist_entry_refuses_before_planning_or_task_mutation(
    tmp_path, monkeypatch, durable,
):
    from redteam.app.bootstrap import run_engagement

    monkeypatch.chdir(tmp_path)
    provider, control = ClosedLoopProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(
        tmp_path, provider, control=control, durable=durable,
    )
    pending = await run_engagement(engagement, deps)
    contender = independent_deps(deps) if durable else deps
    before = await saved_records(deps)
    calls = len(provider.requests)
    async with deps.coordinator.exclusive_run(str(pending.run_id)):
        with pytest.raises(CoordinationError, match="^run_execution_already_owned$"):
            await run_specialists(pending, contender, settings=contender.settings)
    assert await saved_records(deps) == before
    assert len(provider.requests) == calls
    assert not gateway.invocations


async def test_live_owner_refuses_resume_before_reconcile_control_or_model_then_releases(
    tmp_path, monkeypatch,
):
    from redteam.app.bootstrap import run_engagement

    monkeypatch.chdir(tmp_path)
    provider, control = ClosedLoopProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    control.approve(pending)
    cohort = await identities(deps, pending.run_id)
    contender = independent_deps(deps)
    calls, before = len(provider.requests), await saved_records(deps)
    recovered = []
    async with InstanceDriver(deps, settings=deps.settings) as owner:
        await owner.own_run(str(pending.run_id))
        async with InstanceDriver(contender, settings=contender.settings) as driver:
            def recovery_must_not_start(runtime):
                recovered.append(runtime)
                raise AssertionError("refuse before reading/mutating original recovery")
            monkeypatch.setattr(driver, "runner_for", recovery_must_not_start)
            assert await _drive_cohort_guarded(
                driver, SimpleNamespace(run_id=str(pending.run_id)), contender, cohort, cohort,
            ) == 1
        assert not recovered and not control.restored
        assert await saved_records(deps) == before
        assert len(provider.requests) == calls and not gateway.invocations
    # Process ownership release does not replace the original approval/action.
    code, finals = await resume(deps, pending)
    assert code == 0 and len(finals) == 2
    assert len(gateway.invocations) == 2
    assert gateway.invocations[0].action_id == pending.pending_action_id


@pytest.mark.parametrize("durable", [False, True])
async def test_ready_entry_refuses_before_claim_or_subscription(tmp_path, durable):
    _, deps, gateway = await assembled(tmp_path, ClosedLoopProvider(), durable=durable)
    contender = independent_deps(deps) if durable else deps
    base = _runtime(None)
    async with InstanceDriver(deps, settings=deps.settings) as owner:
        await owner.own_run(str(base.run_id))
        before = await saved_records(deps)
        async with InstanceDriver(contender, settings=contender.settings) as driver:
            with pytest.raises(CoordinationError, match="^run_execution_already_owned$"):
                async for _ in driver.drive_ready_tasks(base, max_instances=2, max_activations=4):
                    pytest.fail("competing process may not activate Tasks")
        assert await saved_records(deps) == before
        assert not gateway.invocations


class StopObservedRunner(_FakeRunner):
    def __init__(self, action, stopped):
        super().__init__(action)
        self.stopped = stopped

    def request_stop(self):
        self.stopped.set()
        return True


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_abandoned_iterator_and_close_drain_keep_ownership(tmp_path, durable, cancel_close):
    _, deps, _ = await assembled(tmp_path, ClosedLoopProvider(), durable=durable)
    contender = independent_deps(deps) if durable else deps
    released, stopped = asyncio.Event(), asyncio.Event()

    async def fast(runtime):
        return runtime

    async def slow(runtime):
        await released.wait()
        return runtime

    settings = deps.settings.model_copy(update={"checkpointing": False})
    driver = _StubDriver(deps, settings=settings, runners={
        "fast": StopObservedRunner(fast, stopped), "slow": StopObservedRunner(slow, stopped),
    })
    await driver.open()
    closing = None
    try:
        stream = driver.drive([_runtime("fast"), _runtime("slow")])
        assert (await anext(stream)).instance_id == "fast"
        await stream.aclose()  # The slow actor remains owned, not silently cancelled.
        closing = asyncio.create_task(driver.close())
        await asyncio.wait_for(stopped.wait(), timeout=2)
        with pytest.raises(RuntimeError, match="InstanceDriver is closing"):
            await driver.open()
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
        async with InstanceDriver(contender, settings=settings) as competitor:
            with pytest.raises(CoordinationError, match="^run_execution_already_owned$"):
                await competitor.own_run("run-n")
            assert not closing.done()
            released.set()
            if cancel_close:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, timeout=2)
            else:
                await asyncio.wait_for(closing, timeout=2)
            await competitor.own_run("run-n")
    finally:
        released.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await driver.close()


async def test_driver_idempotent_ownership_cannot_switch_runs_or_acquire_after_close(tmp_path):
    _, deps, _ = await assembled(tmp_path, ClosedLoopProvider())
    driver = InstanceDriver(deps, settings=deps.settings)
    await driver.open()
    await asyncio.gather(driver.own_run("one"), driver.own_run("one"))
    with pytest.raises(CoordinationError, match="^driver_run_ownership_mismatched$"):
        await driver.own_run("other")
    await driver.close()
    with pytest.raises(RuntimeError, match="InstanceDriver is not open"):
        await driver.own_run("one")
    async with deps.coordinator.exclusive_run("one"):
        pass
