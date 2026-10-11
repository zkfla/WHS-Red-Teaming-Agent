"""Original approval resume reconciles only proved immutable evidence writes.

Scripted LLM/synthetic tool fixtures, real SQLite/Filesystem lifecycle.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.attack_graph.stores.sqlite import SQLiteAttackGraphStore
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.coordination import CoordinationError, StateConflict, coordinator_for
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.planning_admission import PlanningAdmission, ReferenceWriteProof
from redteam.orchestration.planning_evidence import PlanningEvidenceStore
from redteam.orchestration.planning_graph import PlanningGraphStore
from redteam.orchestration.runner import AgentLoopRunner
from tests.fakes import FakeEvidenceStore
from tests.integration.test_activation_recovery import NoFollowupProvider
from tests.integration.test_native_specialist_closed_loop import assembled
from tests.integration.test_specialist_resume import OneApprovalControl, resume
from tests.unit.test_planning_graph_recovery import lost_write
from tests.unit.test_planning_write_recovery import pending_append


async def interrupted_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    base = FilesystemEvidenceStore(tmp_path / "original-evidence")
    admission = PlanningAdmission(coordinator)
    deps = replace(deps, evidence_store=PlanningEvidenceStore(base, admission))
    pending = await run_engagement(engagement, deps)
    assert pending.pending_approval_id and not gateway.invocations
    note = EvidenceRecord(
        evidence_id="synthetic-recovery-note", run_id=pending.run_id,
        engagement_id=pending.engagement_id, source_type=EvidenceSourceType.SYSTEM,
        source_id="synthetic-fixture", content_type="application/json", digest="synthetic",
        created_at=datetime.now(timezone.utc), metadata={"fixture_note": True},
    )
    await pending_append(SimpleNamespace(coordinator=coordinator), base, record=note)
    deps = replace(deps, evidence_store=PlanningEvidenceStore(
        FilesystemEvidenceStore(tmp_path / "original-evidence"), admission,
    ))
    return deps, pending, provider, control, gateway, admission, base


async def test_actual_cohort_resume_confirms_fence_before_checkpoint_reconciliation(
    tmp_path, monkeypatch,
):
    deps, pending, provider, control, gateway, admission, base = await interrupted_run(
        tmp_path, monkeypatch,
    )
    original = await deps.evidence_store.list_for_run(pending.run_id)
    original_task = await deps.task_graph_store.get_task(pending.active_task_id)
    ledger = RunRequestLedger(admission.coordinator)
    budget = await ledger.snapshot(str(pending.run_id))
    calls = len(provider.requests)
    actual = AgentLoopRunner.recover_reconciling
    checked = []

    async def assert_finished_fence(runner, *args, **kwargs):
        assert await admission.pending_reference_updates(str(pending.run_id)) == ()
        assert await deps.evidence_store.list_for_run(pending.run_id) == original
        assert await base.verify_run_integrity(pending.run_id) == []
        checked.append(True)
        return await actual(runner, *args, **kwargs)

    monkeypatch.setattr(AgentLoopRunner, "recover_reconciling", assert_finished_fence)
    code, finals = await resume(deps, pending)
    assert code == 0 and checked and len(finals) == 1
    assert finals[0].pending_approval_id == pending.pending_approval_id
    assert finals[0].pending_action_id == pending.pending_action_id
    assert finals[0].instance_id == pending.instance_id
    assert not gateway.invocations and len(provider.requests) == calls
    assert await ledger.snapshot(str(pending.run_id)) == budget
    assert await deps.task_graph_store.get_task(pending.active_task_id) == original_task
    assert not any(outcome.get("actions_consumed", 0) for outcome in control.restored)


@pytest.mark.parametrize("graph_failure", ["error", "cancel", "before"])
async def test_cohort_composes_graph_and_evidence_receipts_without_executing_pending_action(
    tmp_path, monkeypatch, graph_failure,
):
    deps, pending, provider, control, gateway, admission, _ = await interrupted_run(
        tmp_path, monkeypatch,
    )
    await lost_write(deps.graph_store.store, admission, "add_node",
                     failure=graph_failure, run_id=str(pending.run_id))
    deps = replace(deps, graph_store=PlanningGraphStore(
        EngagementScopedAttackGraphStore(SQLiteAttackGraphStore(deps.settings.attack_graph_path),
                                         engagement_id=pending.engagement_id), admission,
    ))
    nodes, edges = await deps.graph_store.list_nodes(), await deps.graph_store.list_edges()
    task = await deps.task_graph_store.get_task(pending.active_task_id)
    ledger = RunRequestLedger(admission.coordinator)
    budget, calls = await ledger.snapshot(str(pending.run_id)), len(provider.requests)
    evidence = await deps.evidence_store.list_for_run(pending.run_id)
    actual = AgentLoopRunner.recover_reconciling
    checked = []

    async def checked_recovery(runner, *args, **kwargs):
        assert await admission.pending_reference_updates(str(pending.run_id)) == ()
        checked.append(True)
        return await actual(runner, *args, **kwargs)

    monkeypatch.setattr(AgentLoopRunner, "recover_reconciling", checked_recovery)
    code, finals = await resume(deps, pending)
    if graph_failure == "before":
        assert code == 1 and not finals and not checked and not control.restored
        (remaining,) = await admission.pending_reference_updates(str(pending.run_id))
        assert remaining.proof.kind == "graph_mutation_v1"
    else:
        assert code == 0 and checked and len(finals) == 1
        assert finals[0].pending_approval_id == pending.pending_approval_id
        assert finals[0].pending_action_id == pending.pending_action_id
        assert finals[0].instance_id == pending.instance_id
    assert await deps.graph_store.list_nodes() == nodes
    assert await deps.graph_store.list_edges() == edges
    assert await deps.evidence_store.list_for_run(pending.run_id) == evidence
    assert await deps.task_graph_store.get_task(pending.active_task_id) == task
    assert await ledger.snapshot(str(pending.run_id)) == budget
    assert not gateway.invocations and len(provider.requests) == calls


async def test_confirmed_writes_do_not_hide_unknown_writer_from_driver(tmp_path, monkeypatch):
    deps, pending, provider, control, gateway, admission, _ = await interrupted_run(
        tmp_path, monkeypatch,
    )
    await lost_write(deps.graph_store.store, admission, "add_node", run_id=str(pending.run_id))
    writer = await admission.begin_reference_update(str(pending.run_id), ["unproved-reference"])
    calls = len(provider.requests)
    async with InstanceDriver(deps) as driver:
        with pytest.raises(StateConflict, match="planning_write_recovery_held"):
            await driver.reconcile_planning_writes(str(pending.run_id))
    (remaining,) = await admission.pending_reference_updates(str(pending.run_id))
    assert remaining.writer == writer and remaining.proof is None
    assert not gateway.invocations and len(provider.requests) == calls and not control.restored


@pytest.mark.parametrize("failure", ["graph", "missing", "unsupported", "no_integrity"])
async def test_unproved_writer_refuses_resume_before_model_tools_or_accounting(
    tmp_path, monkeypatch, failure,
):
    deps, pending, provider, control, gateway, admission, _ = await interrupted_run(
        tmp_path, monkeypatch,
    )
    if failure == "no_integrity":
        deps = replace(deps, evidence_store=PlanningEvidenceStore(FakeEvidenceStore(), admission))
    else:
        refs = ["graph"] if failure == "graph" else ["evidence:unproved"]
        proof = None if failure == "graph" else ReferenceWriteProof(
            kind="not-supported" if failure == "unsupported" else "evidence_append_v1",
            subject_id="absent-original", content_hash="a" * 64,
        )
        await admission.begin_reference_update(str(pending.run_id), refs, proof=proof)
    budget = await RunRequestLedger(admission.coordinator).snapshot(str(pending.run_id))
    calls = len(provider.requests)

    async def forbidden_recovery(*args, **kwargs):
        pytest.fail("held writer must refuse before checkpoint/provider reconciliation")

    monkeypatch.setattr(AgentLoopRunner, "recover_reconciling", forbidden_recovery)
    code, finals = await resume(deps, pending)
    assert code == 1 and not finals
    assert not gateway.invocations and len(provider.requests) == calls
    assert not control.restored
    assert await RunRequestLedger(admission.coordinator).snapshot(str(pending.run_id)) == budget
    assert await admission.pending_reference_updates(str(pending.run_id))


async def test_competing_driver_cannot_confirm_original_writer(tmp_path, monkeypatch):
    deps, pending, _, _, _, admission, _ = await interrupted_run(tmp_path, monkeypatch)
    before = await admission.pending_reference_updates(str(pending.run_id))
    async with InstanceDriver(deps) as owner, InstanceDriver(deps) as other:
        await owner.own_run(str(pending.run_id))
        with pytest.raises(CoordinationError, match="run_execution_already_owned"):
            await other.reconcile_planning_writes(str(pending.run_id))
        assert await admission.pending_reference_updates(str(pending.run_id)) == before
        await owner.reconcile_planning_writes(str(pending.run_id))
    assert await admission.pending_reference_updates(str(pending.run_id)) == ()


async def test_same_driver_refuses_actor_start_and_holds_ownership_until_recovery_finishes(
    tmp_path, monkeypatch,
):
    deps, pending, _, _, _, admission, base = await interrupted_run(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    verify = base.verify_run_integrity

    async def slow_verify(run_id):
        entered.set()
        await release.wait()
        return await verify(run_id)

    # The re-opened wrapper delegates to its own original store, not this base.
    monkeypatch.setattr(deps.evidence_store.store, "verify_run_integrity", slow_verify)
    driver, other = InstanceDriver(deps), InstanceDriver(deps)
    async with driver, other:
        recovering = asyncio.create_task(driver.reconcile_planning_writes(str(pending.run_id)))
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(StateConflict, match="driver_planning_write_recovery_in_progress"):
            await anext(driver.drive([pending]))
        closing = asyncio.create_task(driver.close())
        try:
            # _closed is synchronous; close completion must still await recovery.
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(CoordinationError, match="run_execution_already_owned"):
                await other.own_run(str(pending.run_id))
        finally:
            release.set()
            await recovering
            await closing
        await other.own_run(str(pending.run_id))
    assert await admission.pending_reference_updates(str(pending.run_id)) == ()


async def test_active_actor_refuses_planning_write_recovery(tmp_path, monkeypatch):
    deps, pending, _, _, _, admission, _ = await interrupted_run(tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    class HeldRunner:
        async def run(self, runtime):
            started.set()
            await release.wait()
            return runtime

        def request_stop(self):
            release.set()

    before = await admission.pending_reference_updates(str(pending.run_id))
    async with InstanceDriver(deps) as driver:
        monkeypatch.setattr(driver, "runner_for", lambda runtime: HeldRunner())

        async def drive():
            return [event async for event in driver.drive([pending])]

        actor = asyncio.create_task(drive())
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            with pytest.raises(StateConflict, match="requires_quiescent_driver"):
                await driver.reconcile_planning_writes(str(pending.run_id))
            assert await admission.pending_reference_updates(str(pending.run_id)) == before
        finally:
            release.set()
            await actor
