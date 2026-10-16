"""Approval-wait continuation reservations preserve original execution identity."""

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.policy import Approval
from redteam.core.enums import ApprovalStatus, TaskStatus
from redteam.core.tool_binding import execution_fingerprint
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.central_state import (
    CentralTransaction,
    check_evidence,
    execution_identity_hash,
)
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.state import RuntimeState
from tests.unit.test_central_state import execution, original, owner, world


async def approval_wait(tmp_path):
    state = await world(tmp_path)
    claimed = await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proof = await original(evidence, "proposal-a")
    intent = execution(
        proposal_evidence_id=proof[0].record.evidence_id,
        wait_reason="approval",
        approval_ref="approval-a",
        approval_fingerprint=execution_fingerprint("tool-any", None, {}),
        resource_lease_until=200,
    )
    await state.transaction(lambda tx: tx.record_execution(
        intent, owner="worker-a", checked=proof, now=100,
    ))
    await state.transaction(lambda tx: tx.actor_returned(
        "task-a", owner="worker-a", version=claimed.ownership_version,
        instance_id="instance-a",
    ))
    return state, evidence, intent


async def resource_wait(tmp_path):
    state = await world(tmp_path)
    claimed = await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proof = await original(evidence, "proposal-a")
    intent = execution(
        proposal_evidence_id=proof[0].record.evidence_id,
        wait_reason="resource",
        resource_lease_until=0,
    )
    await state.transaction(lambda tx: tx.record_execution(
        intent, owner="worker-a", checked=proof, now=100,
    ))
    await state.transaction(lambda tx: tx.actor_returned(
        "task-a", owner="worker-a", version=claimed.ownership_version,
        instance_id="instance-a",
    ))
    return state, evidence, intent


def reserve(tx, invocation_ids=("invocation-a",), **overrides):
    options = {
        "owner": "worker-a", "version": 1, "instance_id": "instance-a",
        "approval_invocation_ids": invocation_ids, "now": 100, "max_instances": 1,
        "max_calls": 2, "per_target": 1,
        "comparison_keys": {"invocation-a": ["session-a"]},
    }
    options.update(overrides)
    return tx.reserve_approved_actor("task-a", **options)


@pytest.mark.asyncio
async def test_approved_actor_reservation_keeps_original_pending_intent(tmp_path):
    state, _, original_intent = await approval_wait(tmp_path)

    reserved = await state.transaction(reserve)

    assert reserved is True
    activation = (await state.activation_inventory())[0]
    current = await state.get_execution("invocation-a")
    assert activation["launch_stage"] == "entered"
    assert current == original_intent
    assert current.state is ExecutionState.PENDING
    assert current.wait_reason == "approval"
    assert (await state.get_task("task-a")).blocking_reason == "approval:action-a"


@pytest.mark.asyncio
async def test_reservation_requires_exact_nonempty_pending_approval_set(tmp_path):
    state, _, _ = await approval_wait(tmp_path)

    assert not await state.transaction(lambda tx: reserve(tx, ()))
    assert not await state.transaction(lambda tx: reserve(
        tx, ("invocation-a", "invocation-b"),
    ))
    assert not await state.transaction(lambda tx: reserve(
        tx, ("invocation-a", "invocation-a"),
    ))
    assert not await state.transaction(lambda tx: reserve(
        tx, ("invocation-a", "invocation-b"), max_instances=0,
    ))
    assert await state.transaction(reserve)
    assert (await state.activation_inventory())[0]["launch_stage"] == "entered"


@pytest.mark.asyncio
async def test_unknown_sibling_wait_prevents_approval_actor_reservation(tmp_path):
    state, _, intent = await approval_wait(tmp_path)
    await state.transaction(lambda tx: tx.update_execution(
        intent.model_copy(update={"state": ExecutionState.UNKNOWN}),
        owner="worker-a", now=100,
    ))

    assert not await state.transaction(reserve)
    assert (await state.get_execution("invocation-a")).state is ExecutionState.UNKNOWN
    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"


@pytest.mark.asyncio
async def test_competing_approval_reservations_have_one_winner(tmp_path):
    state, _, _ = await approval_wait(tmp_path)

    results = await asyncio.gather(
        state.transaction(reserve), state.transaction(reserve), return_exceptions=True,
    )

    assert sum(value is True for value in results) == 1
    assert all(value is False or value is True for value in results)
    assert (await state.activation_inventory())[0]["launch_stage"] == "entered"


@pytest.mark.asyncio
async def test_unlaunched_reservation_cleanup_returns_only_exact_entered_slot(tmp_path):
    state, _, intent = await approval_wait(tmp_path)
    assert await state.transaction(reserve)

    assert await state.transaction(lambda tx: tx.release_unlaunched_continuation(
        "task-a", owner="worker-a", version=1, instance_id="instance-a",
    ))
    assert not await state.transaction(lambda tx: tx.release_unlaunched_continuation(
        "task-a", owner="worker-a", version=1, instance_id="instance-a",
    ))
    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"
    assert await state.get_execution("invocation-a") == intent


@pytest.mark.asyncio
async def test_reservation_respects_current_admission_gate(tmp_path):
    state, _, _ = await approval_wait(tmp_path)
    await state.transaction(lambda tx: tx.set_settlement_gate(True))

    with pytest.raises(StateConflict, match="goal_settlement_admission_closed"):
        await state.transaction(reserve)

    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"


async def _add_approval_wait(
    state, evidence, *, invocation_id, action_id, approval_id, resource_key,
):
    proof = await original(
        evidence, f"proposal-{invocation_id}", action=action_id, invocation=invocation_id,
    )
    intent = execution(
        action_id=action_id,
        invocation_id=invocation_id,
        model_tool_call_id=invocation_id.replace("invocation-", "call-"),
        proposal_evidence_id=proof[0].record.evidence_id,
        wait_reason="approval",
        approval_ref=approval_id,
        approval_fingerprint=execution_fingerprint("tool-any", None, {}),
        resource_keys=[resource_key],
        resource_lease_until=200,
    )
    await state.transaction(lambda tx: tx.record_execution(
        intent, owner="worker-a", checked=proof, now=100,
    ))
    return intent


class _RecoveryControl:
    def __init__(self, approvals):
        self.approvals = approvals
        self.inspected = []

    async def inspect_approval(self, approval_id):
        self.inspected.append(str(approval_id))
        return self.approvals.get(str(approval_id))

    async def recorded_decision(self, proposal):
        return None

    async def register_pending_proposal(self, approval_id, proposal):
        return None

    async def rehydrate_run(self, engagement_id, run_id, *, started_at):
        return None


class _ContinuationRunner:
    def __init__(self, runtime):
        self.runtime = runtime

    async def original_central_runtime(self, run_id, instance_id):
        return self.runtime

    async def resume_resource(self, runtime):
        return runtime

    async def run(self, runtime):
        return runtime


def _approval(approval_id, action_id, status):
    return Approval(
        approval_id=approval_id,
        action_id=action_id,
        status=status,
        requested_at=datetime(2026, 10, 11),
        execution_fingerprint=execution_fingerprint("tool-any", None, {}),
    )


def _driver_for(state, control, runner):
    deps = SimpleNamespace(
        central_state=state,
        control_plane=control,
        settings=SimpleNamespace(global_concurrency=4),
        specialist_policy=SimpleNamespace(max_instances=4),
        engagement=SimpleNamespace(
            rate_limit_policy=SimpleNamespace(max_concurrent_actions=4),
            rules_of_engagement=[],
        ),
    )
    driver = InstanceDriver(deps, settings=SimpleNamespace(per_target_concurrency=2))
    driver.runner_for = lambda _runtime: runner
    return driver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_status",
    [ApprovalStatus.PENDING, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED],
)
async def test_every_pending_approval_must_be_approved_before_reservation(
    tmp_path, monkeypatch, second_status,
):
    state, evidence, _ = await approval_wait(tmp_path)
    await state.transaction(lambda tx: tx.resume_actor(
        "task-a", owner="worker-a", version=1, instance_id="instance-a",
        max_instances=1,
    ))
    await state.transaction(lambda tx: tx._write_task(
        tx.get_task("task-a").model_copy(update={
            "status": TaskStatus.RUNNING, "blocking_reason": None,
        })
    ))
    second = await _add_approval_wait(
        state, evidence, invocation_id="invocation-b", action_id="action-b",
        approval_id="approval-b", resource_key="session-b",
    )
    await state.transaction(lambda tx: tx.actor_returned(
        "task-a", owner="worker-a", version=1, instance_id="instance-a",
    ))
    control = _RecoveryControl({
        "approval-a": _approval("approval-a", "action-a", ApprovalStatus.APPROVED),
        "approval-b": _approval("approval-b", "action-b", second_status),
    })
    runtime = RuntimeState(
        run_id="run-central", engagement_id="eng-central", mission_id="mission-central",
        instance_id="instance-a", task_owner="worker-a", task_ownership_version=1,
        active_task_id="task-a",
    )
    runner = _ContinuationRunner(runtime)

    async def resource_keys(_deps, intent):
        return ["session-a" if str(intent.invocation_id) == "invocation-a" else "session-b"]

    monkeypatch.setattr(
        "redteam.orchestration.central_recovery.original_resource_keys", resource_keys,
    )
    driver = _driver_for(state, control, runner)

    builders = await driver._collect_returned_wait_builders(
        runtime, max_instances=4, instance_ids=None, builders=[],
    )

    assert builders == []
    assert set(control.inspected) == {"approval-a", "approval-b"}
    assert await state.get_execution(str(second.invocation_id)) == second
    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"


@pytest.mark.asyncio
async def test_unresolved_job_sibling_blocks_approval_reservation(tmp_path):
    state = await world(tmp_path)
    claimed = await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    job_proof = await original(
        evidence, "proposal-invocation-z", action="action-z", invocation="invocation-z",
    )
    job = execution(
        action_id="action-z", invocation_id="invocation-z", model_tool_call_id="call-z",
        proposal_evidence_id=job_proof[0].record.evidence_id,
        resource_keys=[], resource_lease_until=0, state=ExecutionState.UNKNOWN,
        wait_reason="job", job_ref="job-z",
    )
    await state.transaction(lambda tx: tx.db.execute(
        "INSERT INTO state_executions VALUES(?,?,?,?,?)",
        ("run-central", "invocation-z", "task-a", execution_identity_hash(job),
         job.model_dump_json()),
    ))
    await _add_approval_wait(
        state, evidence, invocation_id="invocation-a", action_id="action-a",
        approval_id="approval-a", resource_key="session-a",
    )
    await state.transaction(lambda tx: tx.actor_returned(
        "task-a", owner="worker-a", version=claimed.ownership_version,
        instance_id="instance-a",
    ))

    assert (await state.get_task("task-a")).blocking_reason == "approval:action-a"
    assert not await state.transaction(reserve)
    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"


@pytest.mark.asyncio
async def test_initial_drain_rejection_releases_unlaunched_approval_reservation(tmp_path):
    state, _, _ = await approval_wait(tmp_path)
    assert await state.transaction(reserve)
    runtime = RuntimeState(
        run_id="run-central", engagement_id="eng-central", mission_id="mission-central",
        instance_id="instance-a", task_owner="worker-a", task_ownership_version=1,
        active_task_id="task-a",
    )
    runner = _ContinuationRunner(runtime)
    deps = SimpleNamespace(
        central_state=state,
        settings=SimpleNamespace(global_concurrency=1),
        specialist_policy=SimpleNamespace(max_instances=1),
    )
    driver = InstanceDriver(deps, settings=SimpleNamespace(per_target_concurrency=1))
    builders = [
        (runtime.instance_id, runner, runtime, runner.resume_resource),
        ("instance-b", runner, runtime.model_copy(update={"instance_id": "instance-b"}),
         runner.run),
    ]

    with pytest.raises(StateConflict, match="central_worker_limit_reached"):
        async for _ in driver._drain_as_ready(builders):
            pass

    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"
    assert await state.get_execution("invocation-a") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_kind", ["approval", "resource"])
async def test_cancellation_during_reservation_commit_releases_unlaunched_slot(
    tmp_path, monkeypatch, wait_kind,
):
    state, _, intent = (await approval_wait(tmp_path) if wait_kind == "approval"
                        else await resource_wait(tmp_path))
    approvals = ({
        "approval-a": _approval("approval-a", "action-a", ApprovalStatus.APPROVED),
    } if wait_kind == "approval" else {})
    control = _RecoveryControl(approvals)
    runtime = RuntimeState(
        run_id="run-central", engagement_id="eng-central", mission_id="mission-central",
        instance_id="instance-a", task_owner="worker-a", task_ownership_version=1,
        active_task_id="task-a",
    )
    runner = _ContinuationRunner(runtime)

    async def resource_keys(_deps, _intent):
        return ["session-a"]

    monkeypatch.setattr(
        "redteam.orchestration.central_recovery.original_resource_keys", resource_keys,
    )
    reservation_entered = threading.Event()
    release_commit = threading.Event()
    original_reserve = CentralTransaction._reserve_returned_actor

    def hold_before_commit(tx, *args, **kwargs):
        reserved = original_reserve(tx, *args, **kwargs)
        if reserved:
            reservation_entered.set()
            if not release_commit.wait(timeout=5):
                raise TimeoutError("reservation commit fixture timed out")
        return reserved

    monkeypatch.setattr(CentralTransaction, "_reserve_returned_actor", hold_before_commit)
    driver = _driver_for(state, control, runner)
    scanning = asyncio.create_task(driver._collect_returned_wait_builders(
        runtime, max_instances=4, instance_ids=None, builders=[],
    ))
    try:
        assert await asyncio.to_thread(reservation_entered.wait, 5)
        scanning.cancel()
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await scanning
    finally:
        release_commit.set()
        if not scanning.done():
            scanning.cancel()
            await asyncio.gather(scanning, return_exceptions=True)

    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"
    assert await state.get_execution(str(intent.invocation_id)) == intent


@pytest.mark.asyncio
async def test_recovered_approval_allows_older_execution_epochs(tmp_path, monkeypatch):
    state, evidence, intent = await approval_wait(tmp_path)
    sibling_proof = await original(
        evidence, "proposal-invocation-z", action="action-z", invocation="invocation-z",
    )
    sibling_result = await original(
        evidence, "result-invocation-z", action="action-z", invocation="invocation-z",
    )
    resolved_sibling = execution(
        action_id="action-z", invocation_id="invocation-z", model_tool_call_id="call-z",
        proposal_evidence_id=sibling_proof[0].record.evidence_id,
        result_evidence_id=sibling_result[0].record.evidence_id,
        resource_keys=[], resource_lease_until=0, state=ExecutionState.SUCCEEDED,
    )
    await state.transaction(lambda tx: tx.db.execute(
        "INSERT INTO state_executions VALUES(?,?,?,?,?)",
        ("run-central", "invocation-z", "task-a", execution_identity_hash(resolved_sibling),
         resolved_sibling.model_dump_json()),
    ))
    originals = await check_evidence(
        evidence, run_id="run-central", engagement_id="eng-central",
        evidence_ids=[str(intent.proposal_evidence_id),
                      str(sibling_proof[0].record.evidence_id),
                      str(sibling_result[0].record.evidence_id)],
    )
    transferred = await state.transaction(lambda tx: tx.recover_actor(
        "task-a", previous_owner="worker-a", version=1, instance_id="instance-a",
        owner="worker-b", checked=originals,
    ))
    assert transferred.ownership_version == 2

    control = _RecoveryControl({
        "approval-a": _approval("approval-a", "action-a", ApprovalStatus.APPROVED),
    })
    runtime = RuntimeState(
        run_id="run-central", engagement_id="eng-central", mission_id="mission-central",
        instance_id="instance-a", task_owner="worker-b", task_ownership_version=2,
        active_task_id="task-a",
    )
    runner = _ContinuationRunner(runtime)

    async def resource_keys(_deps, _intent):
        return ["session-a"]

    monkeypatch.setattr(
        "redteam.orchestration.central_recovery.original_resource_keys", resource_keys,
    )
    driver = _driver_for(state, control, runner)
    builders = await driver._collect_returned_wait_builders(
        runtime, max_instances=4, instance_ids=None, builders=[],
    )

    assert len(builders) == 1
    assert (await state.activation_inventory())[0]["launch_stage"] == "entered"
    assert (await state.get_execution("invocation-a")).ownership_version == 1
    assert (await state.get_execution("invocation-z")).ownership_version == 1


@pytest.mark.asyncio
async def test_future_execution_epoch_blocks_approval_reservation(tmp_path):
    state, _, intent = await approval_wait(tmp_path)
    future = intent.model_copy(update={"ownership_version": 2})
    await state.transaction(lambda tx: tx.db.execute(
        "UPDATE state_executions SET identity_hash=?,payload=? "
        "WHERE run_id=? AND invocation_id=?",
        (execution_identity_hash(future), future.model_dump_json(), "run-central",
         str(future.invocation_id)),
    ))

    assert not await state.transaction(reserve)
    assert (await state.activation_inventory())[0]["launch_stage"] == "returned"
