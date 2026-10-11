"""Versioned zero-entry proofs are not TTLs, releases, or request refunds."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.coordination import CoordinationError, StateConflict
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_activation import TaskActivation
from tests.integration.test_ready_task_activation import prepared
from tests.unit.test_orchestration_coordination import MISSION, RUN
from tests.unit.test_run_execution_ownership import sqlite_coordinator


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_read_only_reserved_inventory_and_entry_marker_keep_original_claim(tmp_path, backend):
    w, activation, base = await prepared(tmp_path, backend)
    ledger = RunRequestLedger(activation.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=3))
    (original,) = await activation.ready(base, max_instances=1, max_activations=2)
    before = await ledger.snapshot(RUN)
    recovered = TaskActivation(activation.coordinator, activation.agents)
    assert await recovered.unstarted(RUN, checkpoint_instances=set()) == [original]
    assert await recovered.unstarted(RUN, checkpoint_instances=set()) == [original]
    assert await ledger.snapshot(RUN) == before
    assert (await w.tasks.get_task(original.active_task_id)).occupant_owner == original.work_owner
    await recovered.entering(original)
    with pytest.raises(StateConflict, match="^activation_checkpoint_required$"):
        await recovered.unstarted(RUN, checkpoint_instances=set())
    assert await recovered.unstarted(RUN, checkpoint_instances={str(original.instance_id)}) == []
    assert await activation.ready(base, max_instances=1, max_activations=2) == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("change", [
    "legacy", "entered", "held", "runtime", "claim", "contract", "role", "capability",
    "goal", "generation", "input_binding", "request", "released_request", "closed", "pending",
])
async def test_changed_or_unprovable_reservation_never_returns_fresh_runtime(
    tmp_path, backend, change,
):
    w, activation, base = await prepared(tmp_path, backend)
    ledger = RunRequestLedger(activation.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=3))
    (original,) = await activation.ready(base, max_instances=1, max_activations=2)

    async def mutate(tx):
        row = tx.records[("task_activation", f"{RUN}:{original.active_task_id}")]
        if change == "legacy":
            row.pop("launch_stage")
            row.pop("launch_proof_version")
        elif change == "entered":
            row["launch_stage"] = "entered"
        elif change == "held":
            row["state"] = "held"
        elif change == "runtime":
            row["runtime"]["actions_executed"] = 1
        elif change in {"claim", "contract", "role", "capability"}:
            field, value = {
                "claim": ("occupant_owner", "different-instance"),
                "contract": ("objective", "Changed authorized investigation"),
                "role": ("assigned_agent", "recon-agent"),
                "capability": ("required_capability", "recon"),
            }[change]
            task = await tx.tasks.get_task(original.active_task_id)
            await tx.tasks.update_task(task.model_copy(update={field: value}))
        elif change == "goal":
            mission = tx.tasks.peek_mission(MISSION)
            await tx.tasks.update_mission(mission.model_copy(update={
                "objective": "Changed goal", "revision": mission.revision + 1,
            }))
        elif change == "generation":
            tx.records[("generation", f"{RUN}:{MISSION}")] = 1
        elif change == "input_binding":
            tx.records[("planning_inputs", RUN)] = "changed"
        elif change == "closed":
            tx.records[("binding", RUN)]["closed"] = True

    await activation.coordinator.change(mutate)
    if change in {"request", "released_request"}:
        request = await ledger.reserve(RUN, owner=original.work_owner, kind="synthetic")
        if change == "released_request":
            await ledger.release_undispatched(RUN, request)
    if change == "pending":
        await w.service.receive("stop", mission_id=MISSION, run_id=RUN, publisher=w.publisher)
    before = await ledger.snapshot(RUN)
    with pytest.raises((StateConflict, CoordinationError)):
        await activation.unstarted(RUN, checkpoint_instances=set())
    assert await ledger.snapshot(RUN) == before


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_unrelated_sibling_change_does_not_invalidate_reserved_actor(tmp_path, backend):
    w, activation, base = await prepared(tmp_path, backend)
    (original,) = await activation.ready(base, max_instances=1, max_activations=2)
    sibling = next(task for task in await w.tasks.list_tasks()
                   if task.task_id != original.active_task_id)
    await w.tasks.update_task(sibling.model_copy(update={"objective": "Independent followup"}))
    assert await activation.unstarted(RUN, checkpoint_instances=set()) == [original]


_CRASH = """
import asyncio, json, os, sys
from pathlib import Path
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.llm_requests import RunRequestLedger
from tests.integration.test_ready_task_activation import prepared
async def main():
    _, activation, base = await prepared(Path(sys.argv[1]), 'sqlite')
    async with activation.coordinator.exclusive_run(str(base.run_id)):
        ledger = RunRequestLedger(activation.coordinator)
        await ledger.bind(str(base.run_id), LLMRequestPolicy(mode='bounded', max_requests=4))
        request = await ledger.reserve(str(base.run_id), owner='planner', kind='synthetic')
        await ledger.dispatch(str(base.run_id), request)
        await ledger.settle(str(base.run_id), request, unknown=True)
        (runtime,) = await activation.ready(base, max_instances=1, max_activations=2)
        if sys.argv[2] == 'entered':
            await activation.entering(runtime)
        print(json.dumps(runtime.model_dump(mode='json')), flush=True)
        os._exit(17)  # controlled process crash, not a generated/target program
asyncio.run(main())
"""


@pytest.mark.parametrize("stage", ["reserved", "entered"])
async def test_real_process_crash_preserves_identity_claim_clock_and_unknown_usage(tmp_path, stage):
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _CRASH, str(tmp_path), stage,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        # Imports fixtures from the checkout, not an external execution tool.
        cwd=str(Path(__file__).parents[2]),
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=10)
        assert child.returncode == 17, stderr.decode()
        assert child.pid != os.getpid()
        original = RuntimeState.model_validate(json.loads(stdout))
        coordinator = sqlite_coordinator(tmp_path / "coordination.sqlite")
        activation = TaskActivation(coordinator, default_registry())
        async with coordinator.exclusive_run(RUN):
            if stage == "reserved":
                assert await activation.unstarted(RUN, checkpoint_instances=set()) == [original]
            else:
                with pytest.raises(StateConflict, match="^activation_checkpoint_required$"):
                    await activation.unstarted(RUN, checkpoint_instances=set())
            after = await RunRequestLedger(coordinator).snapshot(RUN)
            assert after["spent"] == 1 and after["unknown_dispatch"] == 1
            assert after["policy"] == original.llm_request_policy
            async def claim(tx):
                return await tx.tasks.get_task(original.active_task_id)
            assert (await coordinator.change(claim)).occupant_owner == original.work_owner
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.communicate(), timeout=5)
