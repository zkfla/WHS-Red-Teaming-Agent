"""Explicit original collection is not resume/claim handoff (synthetic only)."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.tool import ToolInvocation
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.coordination import CoordinationError, StateConflict
from redteam.orchestration.execution_journal import append_execution_receipt
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from tests.integration.test_activation_recovery import (
    NoFollowupProvider,
    drive_resume,
    reserved_peer,
)
from tests.integration.test_execution_receipt_reconciliation import _world
from tests.integration.test_native_specialist_closed_loop import assembled
from tests.integration.test_specialist_resume import OneApprovalControl


async def collection_world(tmp_path, durable):
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(
        tmp_path, provider, control=control, durable=durable,
    )
    if not durable:
        deps = replace(deps, settings=deps.settings.model_copy(update={"checkpointing": True}))
    pending = await run_engagement(engagement, deps)
    activation, peer = await reserved_peer(deps, pending)
    await activation.entering(peer)  # possible entry is not zero-use proof
    _other, recovery, store, old, _work = await _world(tmp_path / "original-provider", late=True)
    source = next(record for record in await store.list_for_run(old.run_id)
                  if record.metadata.get("receipt_kind") == "execution_submission")
    invocation = ToolInvocation.model_validate(source.metadata["invocation"])
    invocation = invocation.model_copy(update={
        "run_id": peer.run_id,
        "execution_context": invocation.execution_context.model_copy(update={
            "run_id": peer.run_id, "engagement_id": peer.engagement_id,
            "instance_id": peer.instance_id, "agent_id": peer.current_agent,
        }),
    })
    proposal = ActionProposal.model_validate(source.metadata["proposal"]).model_copy(update={
        "run_id": peer.run_id, "engagement_id": peer.engagement_id,
        "instance_id": peer.instance_id, "agent_id": peer.current_agent,
    })
    step = PlanStep.model_validate(source.metadata["plan_step"]).model_copy(update={
        "mission_task_id": str(peer.active_task_id),
    })
    await append_execution_receipt(
        deps.evidence_store, invocation=invocation, proposal=proposal, step=step, work_id=None,
        reference=recovery.prepare_reference(invocation),
    )
    deps = replace(deps, execution_recovery=recovery)
    return deps, activation, pending, peer, provider, control, gateway, recovery


@pytest.mark.parametrize("durable", [False, True])
async def test_entered_no_checkpoint_collection_keeps_claim_ledger_and_resume_held(
    tmp_path, monkeypatch, durable,
):
    monkeypatch.chdir(tmp_path)
    deps, activation, pending, peer, provider, control, gateway, recovery = (
        await collection_world(tmp_path, durable)
    )
    ledger = RunRequestLedger(activation.coordinator)
    before = await ledger.snapshot(str(peer.run_id))
    task_before = await deps.task_graph_store.get_task(peer.active_task_id)
    calls = len(provider.requests)
    async with InstanceDriver(deps) as driver:
        result = await driver.collect_checkpointless_originals(
            str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
        )
        assert len(result) == 1 and result[0].instance_id == peer.instance_id
        assert [row.state for row in result[0].originals] == ["known"]
        assert not result[0].claim_release_permitted
        assert not await driver.runner_for(peer).checkpoint_exists(peer)
        again = await driver.collect_checkpointless_originals(
            str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
        )
        assert again == result and recovery.queries == recovery.fetches == 1
    assert await ledger.snapshot(str(peer.run_id)) == before
    assert await deps.task_graph_store.get_task(peer.active_task_id) == task_before
    assert len(provider.requests) == calls and not gateway.invocations
    if durable:
        control.approve(pending)
        code, finals = await drive_resume(deps, pending)
        assert code == 1 and not finals
        assert not control.restored
    with pytest.raises(StateConflict, match="activation_checkpoint_required"):
        await activation.unstarted(
            str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
        )


@pytest.mark.parametrize("condition", [
    "claim", "runtime", "legacy", "configuration", "component", "policy", "checkpoint",
])
async def test_unproved_actor_refused_before_original_query(tmp_path, monkeypatch, condition):
    monkeypatch.chdir(tmp_path)
    deps, activation, pending, peer, _provider, _control, _gateway, recovery = (
        await collection_world(tmp_path, True)
    )
    if condition == "component":
        deps = replace(deps, component_binding=None)
    elif condition == "policy":
        deps = replace(deps, settings=deps.settings.model_copy(update={"llm_request_policy": None}))
    elif condition == "claim":
        task = await deps.task_graph_store.get_task(peer.active_task_id)
        await deps.task_graph_store.update_task(task.model_copy(update={
            "occupant_owner": "foreign",
        }))
    elif condition in {"runtime", "legacy", "configuration"}:
        async def corrupt(tx):
            if condition == "configuration":
                tx.records[("planning_inputs", str(peer.run_id))] = "foreign"
            else:
                row = tx.records[("task_activation", f"{peer.run_id}:{peer.active_task_id}")]
                if condition == "runtime":
                    row["runtime"]["actions_executed"] = 99
                else:
                    row.pop("launch_proof_version")
        await activation.coordinator.change(corrupt)
    async with InstanceDriver(deps) as driver:
        if condition == "checkpoint":
            monkeypatch.setattr(driver.runner_for(peer), "checkpoint_exists", AsyncMock(
                return_value=True,
            ))
        with pytest.raises(StateConflict):
            await driver.collect_checkpointless_originals(
                str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
            )
    assert recovery.queries == recovery.fetches == 0


async def test_collection_cannot_use_other_driver_or_start_while_query_is_in_flight(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    deps, _activation, pending, peer, _provider, _control, _gateway, recovery = (
        await collection_world(tmp_path, True)
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    original_query = recovery.query_execution
    async def paused_query(reference):
        entered.set()
        await finish.wait()
        return await original_query(reference)
    recovery.query_execution = paused_query
    async with InstanceDriver(deps) as driver, InstanceDriver(deps) as other:
        job = asyncio.create_task(driver.collect_checkpointless_originals(
            str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(CoordinationError, match="run_execution_already_owned"):
                await other.collect_checkpointless_originals(
                    str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
                )
            with pytest.raises(StateConflict, match="recovery_in_progress"):
                driver.drive([peer])
        finally:
            finish.set()
            result = await job
        assert result[0].originals[0].state == "known"


async def test_collection_empty_originals_never_means_unentered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, _gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    activation, peer = await reserved_peer(deps, pending)
    await activation.entering(peer)
    async with InstanceDriver(deps) as driver:
        (result,) = await driver.collect_checkpointless_originals(
            str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
        )
        assert result.originals == [] and not result.replay_permitted
        with pytest.raises(StateConflict, match="activation_checkpoint_required"):
            await driver.recover_unstarted_activations(
                str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
            )
