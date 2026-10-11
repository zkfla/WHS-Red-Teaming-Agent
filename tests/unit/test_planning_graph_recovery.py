"""Interrupted graph writes require the original atomic receipt, never replay."""

import asyncio
import sqlite3
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.attack_graph.stores.sqlite import SQLiteAttackGraphStore
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.planning_graph import PlanningGraphStore
from tests.unit.test_attack_graph_sqlite_store import edge, node
from tests.unit.test_orchestration_coordination import MISSION, RUN, world
from tests.unit.test_planning_write_recovery import reopen


async def lost_write(store, admission, operation, *, failure="error", run_id=RUN):
    class LostAcknowledgement:
        def __getattr__(self, name):
            return getattr(store, name)

        async def apply_graph_mutation(self, mutation):
            if failure != "before":
                await store.apply_graph_mutation(mutation)
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("synthetic private acknowledgement loss")

    wrapped = PlanningGraphStore(LostAcknowledgement(), admission)
    value = node("host:a") if operation == "add_node" else (
        edge("edge:one", "host:a", "host:b") if operation == "add_edge"
        else "host:a" if operation == "remove_node" else "edge:one"
    )
    with llm_request_scope(run_id, "synthetic-writer"):
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else OSError):
            await getattr(wrapped, operation)(value)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("operation", ["add_node", "add_edge", "remove_node", "remove_edge"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_original_receipt_clears_only_its_fence_after_restart(
    tmp_path, backend, scoped, operation, failure,
):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    await admission.bind_inputs(RUN, {"scope": "synthetic"})
    stamp = await admission.capture(RUN, MISSION, reference_keys=["graph"])
    path = tmp_path / "graph.sqlite3"
    base = SQLiteAttackGraphStore(path)
    store = EngagementScopedAttackGraphStore(base, engagement_id="eng-a") if scoped else base
    await store.add_node(node("host:a"))
    await store.add_node(node("host:b"))
    await store.add_edge(edge("edge:one", "host:a", "host:b"))
    await lost_write(store, admission, operation, failure=failure)
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["graph"])
    tasks = await w.tasks.list_tasks(mission_id=MISSION)
    # Current values can legitimately differ from the accepted original write.
    await store.add_node(node("host:a", labels=["later"]))
    base = SQLiteAttackGraphStore(path)
    store = EngagementScopedAttackGraphStore(base, engagement_id="eng-a") if scoped else base
    nodes, edges = await store.list_nodes(), await store.list_edges()
    coordinator = reopen(w, backend)
    admission = PlanningAdmission(coordinator)
    wrapped = PlanningGraphStore(store, admission)
    async with coordinator.exclusive_run(RUN):
        (outcome,) = await wrapped.reconcile_pending_writes(RUN)
        assert outcome.state == "confirmed"
        assert outcome.reason_code == "original_graph_mutation_confirmed"
        assert await wrapped.reconcile_pending_writes(RUN) == ()
    assert await store.list_nodes() == nodes and await store.list_edges() == edges
    assert await w.tasks.list_tasks(mission_id=MISSION) == tasks
    assert await admission.capture_references(RUN, ["graph"]) == (("graph", 1),)

    async def forbidden(tx):
        pytest.fail("recovery must not admit an old plan")

    with pytest.raises(StateConflict, match="stale_plan_reference_changed"):
        await admission.admit(stamp, proposal_id="late", proposal={}, apply=forbidden)


async def test_matching_graph_without_receipt_does_not_prove_original_acceptance(tmp_path):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    store = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")
    await store.add_node(node("host:a"))
    await lost_write(store, admission, "add_node", failure="before")
    async with w.coordinator.exclusive_run(RUN):
        (outcome,) = await PlanningGraphStore(store, admission).reconcile_pending_writes(RUN)
    assert outcome.reason_code == "original_graph_mutation_receipt_missing"
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["graph"])


@pytest.mark.parametrize("case,reason", [
    ("wrong_run", "original_graph_mutation_receipt_conflict"),
    ("wrong_id", "original_graph_mutation_receipt_conflict"),
    ("wrong_hash", "original_graph_mutation_receipt_conflict"),
    ("wrong_refs", "graph_reference_inventory_conflict"),
    ("read_failed", "graph_mutation_receipt_read_failed"),
    ("invalid_schema", "graph_mutation_receipt_read_failed"),
    ("invalid_result", "graph_mutation_receipt_read_failed"),
    ("index_corrupt", "graph_mutation_receipt_read_failed"),
    ("wrong_namespace", "original_graph_mutation_receipt_missing"),
    ("no_receipt_port", "graph_mutation_receipt_unavailable"),
])
async def test_unproved_original_never_clears_writer(tmp_path, monkeypatch, case, reason):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    path = tmp_path / "graph.sqlite3"
    base = SQLiteAttackGraphStore(path)
    store = EngagementScopedAttackGraphStore(base, engagement_id="eng-a")
    await lost_write(store, admission, "add_node")
    read = store.read_graph_mutation_receipt
    if case == "wrong_namespace":
        store = EngagementScopedAttackGraphStore(base, engagement_id="different-eng")
    elif case == "no_receipt_port":
        store = object()
    elif case == "index_corrupt":
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE graph_mutation_receipts SET receipt=json_set(receipt, '$.run_id', 'other')",
            )
    else:
        async def changed(*args, **kwargs):
            receipt = await read(*args, **kwargs)
            if case == "read_failed":
                raise OSError("private payload")
            field, value = {
                "wrong_run": ("run_id", "other-run"), "wrong_id": ("mutation_id", "other-id"),
                "wrong_hash": ("request_hash", "a" * 64), "wrong_refs": ("references", ("graph",)),
                "invalid_schema": ("schema_version", 2),
                "invalid_result": ("result", True),
            }[case]
            return receipt.model_copy(update={field: value})
        monkeypatch.setattr(store, "read_graph_mutation_receipt", changed)
    async with w.coordinator.exclusive_run(RUN):
        (outcome,) = await PlanningGraphStore(store, admission).reconcile_pending_writes(RUN)
    assert outcome.state == "held" and outcome.reason_code == reason
    assert "private payload" not in outcome.model_dump_json()
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["graph"])


@pytest.mark.parametrize("mutation", ["version", "writer", "proof", "extra_reference"])
async def test_cas_change_during_receipt_read_preserves_fence(tmp_path, mutation):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    store = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")
    await lost_write(store, admission, "add_node")
    (intent,) = await admission.pending_reference_updates(RUN)

    class ChangingStore:
        def __getattr__(self, name):
            return getattr(store, name)

        async def read_graph_mutation_receipt(self, *args, **kwargs):
            receipt = await store.read_graph_mutation_receipt(*args, **kwargs)

            async def change(tx):
                row = tx.records[("planning_reference", f"{RUN}:graph")]
                if mutation == "version":
                    row["version"] += 1
                elif mutation == "writer":
                    row["writer"] = "new-writer"
                elif mutation == "proof":
                    saved = tx.records[("planning_writer", f"{RUN}:{intent.writer}")]
                    saved["proof"]["content_hash"] = "a" * 64
                else:
                    tx.records[("planning_reference", f"{RUN}:extra")] = {
                        "version": 1, "writer": intent.writer,
                    }
            await w.coordinator.change(change)
            return receipt

    async with w.coordinator.exclusive_run(RUN):
        wrapper = PlanningGraphStore(ChangingStore(), admission)
        (outcome,) = await wrapper.reconcile_pending_writes(RUN)
    assert outcome.reason_code == "reference_update_changed_during_recovery"
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["node:host:a"])


async def test_cancelled_receipt_read_and_foreign_run_do_not_clear_original(tmp_path, monkeypatch):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    store = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")
    await lost_write(store, admission, "add_node")
    other = await admission.begin_reference_update("other-run", ["graph"])
    monkeypatch.setattr(store, "read_graph_mutation_receipt",
                        AsyncMock(side_effect=asyncio.CancelledError))
    async with w.coordinator.exclusive_run(RUN):
        with pytest.raises(asyncio.CancelledError):
            await PlanningGraphStore(store, admission).reconcile_pending_writes(RUN)
    assert (await admission.pending_reference_updates("other-run"))[0].writer == other
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["graph"])


async def test_caller_mutation_during_prepare_cannot_change_expected_write(tmp_path):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    store = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowPreparation:
        def __getattr__(self, name):
            return getattr(store, name)

        async def prepare_graph_mutation(self, request):
            entered.set()
            await release.wait()
            return await store.prepare_graph_mutation(request)

    original = node("host:a", labels=["before"])
    wrapper = PlanningGraphStore(SlowPreparation(), admission)
    with llm_request_scope(RUN, "synthetic-writer"):
        writing = asyncio.create_task(wrapper.add_node(original))
        await asyncio.wait_for(entered.wait(), timeout=5)
        original.labels.append("late-mutation")
        release.set()
        await writing
    assert (await store.get_node("host:a")).labels == ["before"]
    assert await admission.pending_reference_updates(RUN) == ()


async def test_unsupported_provider_keeps_ordinary_writes_and_unknown_fence(tmp_path):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    base = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")

    class LegacyStore:
        def __getattr__(self, name):
            if name in {"prepare_graph_mutation", "apply_graph_mutation",
                        "read_graph_mutation_receipt"}:
                raise AttributeError(name)
            return getattr(base, name)

        async def add_node(self, value):
            await base.add_node(value)
            raise OSError("synthetic lost legacy acknowledgement")

    wrapper = PlanningGraphStore(LegacyStore(), admission)
    with llm_request_scope(RUN, "synthetic-legacy"):
        with pytest.raises(OSError):
            await wrapper.add_node(node("host:a"))
    assert await base.get_node("host:a") == node("host:a")
    async with w.coordinator.exclusive_run(RUN):
        (outcome,) = await wrapper.reconcile_pending_writes(RUN)
    assert outcome.state == "held" and outcome.reason_code == "reference_update_proof_missing"


async def test_preparation_must_keep_original_run_operation_and_references(tmp_path, monkeypatch):
    w = await world(tmp_path, "sqlite")
    admission = PlanningAdmission(w.coordinator)
    base = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")
    prepare = base.prepare_graph_mutation

    async def malformed(request):
        return (await prepare(request)).model_copy(update={"run_id": "different-run"})

    monkeypatch.setattr(base, "prepare_graph_mutation", malformed)
    with llm_request_scope(RUN, "synthetic-writer"):
        with pytest.raises(StateConflict, match="preparation_conflict"):
            await PlanningGraphStore(base, admission).add_node(node("host:a"))
    assert await admission.pending_reference_updates(RUN) == ()
    assert not base._path.exists()


async def test_real_process_exit_after_graph_commit_recovers_without_replay(tmp_path):
    w = await world(tmp_path, "sqlite")
    path = tmp_path / "graph.sqlite3"
    script = '''
import asyncio, os, sys
from redteam.attack_graph.stores.sqlite import SQLiteAttackGraphStore
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.planning_graph import PlanningGraphStore
from redteam.orchestration.metered_llm import llm_request_scope
from tests.unit.test_attack_graph_sqlite_store import node
class ExitAfterCommit:
    def __init__(self): self.store = SQLiteAttackGraphStore(sys.argv[2])
    def __getattr__(self, name): return getattr(self.store, name)
    async def apply_graph_mutation(self, mutation):
        await self.store.apply_graph_mutation(mutation)
        os._exit(0)
async def main():
    coordinator = coordinator_for(SQLiteTaskGraphStore(sys.argv[1]), SQLitePathStore(sys.argv[1]))
    graph = PlanningGraphStore(ExitAfterCommit(), PlanningAdmission(coordinator))
    with llm_request_scope(sys.argv[3], "synthetic-process"):
        await graph.add_node(node("host:a"))
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, "-c", script, str(w.db), str(path), RUN],
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    coordinator = reopen(w, "sqlite")
    admission = PlanningAdmission(coordinator)
    assert await admission.pending_reference_updates(RUN)
    store = SQLiteAttackGraphStore(path)
    async with coordinator.exclusive_run(RUN):
        (outcome,) = await PlanningGraphStore(store, admission).reconcile_pending_writes(RUN)
    assert outcome.state == "confirmed" and await store.get_node("host:a") == node("host:a")
