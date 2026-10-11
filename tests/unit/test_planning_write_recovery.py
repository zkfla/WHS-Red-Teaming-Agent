"""Confirmed immutable appends recover fences, never execution permissions."""

import asyncio
import sqlite3
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.coordination import StateConflict, content_hash, coordinator_for
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.planning_admission import PlanningAdmission, ReferenceWriteProof
from redteam.orchestration.planning_evidence import PlanningEvidenceStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.fakes import FakeEvidenceStore
from tests.unit.test_orchestration_coordination import MISSION, RUN, world
from tests.unit.test_planning_evidence import binding


def proof(record):
    return ReferenceWriteProof(kind="evidence_append_v1", subject_id=str(record.evidence_id),
                               content_hash=content_hash(record))


async def pending_append(w, base, *, failure="error", record=None):
    class LostAcknowledgement:
        def __getattr__(self, name):
            return getattr(base, name)

        async def append(self, record):
            await base.append(record)
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("synthetic lost acknowledgement; private payload")

    store = PlanningEvidenceStore(LostAcknowledgement(), PlanningAdmission(w.coordinator))
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else OSError):
        await store.append(record or binding())


def reopen(w, backend):
    return (coordinator_for(SQLiteTaskGraphStore(w.db), SQLitePathStore(w.db))
            if backend == "sqlite" else w.coordinator)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_exact_append_recovery_preserves_originals_tasks_and_stale_plan(
    tmp_path, backend, failure,
):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    await admission.bind_inputs(RUN, {"scope": "synthetic"})
    stamp = await admission.capture(RUN, MISSION, reference_keys=["evidence"])
    root = tmp_path / "evidence"
    base = FilesystemEvidenceStore(root)
    await pending_append(w, base, failure=failure)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    tasks = await w.tasks.list_tasks(mission_id=MISSION)
    coordinator = reopen(w, backend)
    admission = PlanningAdmission(coordinator)
    store = PlanningEvidenceStore(FilesystemEvidenceStore(root), admission)
    await store.append(await base.get("binding"))  # idempotency alone must NOT clear a fence
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["evidence"])
    async with coordinator.exclusive_run(RUN):
        (outcome,) = await store.reconcile_pending_writes(RUN)
        assert outcome.state == "confirmed"
        assert outcome.reason_code == "original_evidence_append_confirmed"
        assert await store.reconcile_pending_writes(RUN) == ()
    assert await admission.capture_references(RUN, ["evidence", "validity:original"]) == (
        ("evidence", 1), ("validity:original", 1),
    )
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert await w.tasks.list_tasks(mission_id=MISSION) == tasks

    async def apply(tx):
        pytest.fail("old plans must remain stale, not be admitted by fence recovery")

    with pytest.raises(StateConflict, match="stale_plan_reference_changed"):
        await admission.admit(stamp, proposal_id="late", proposal={}, apply=apply)
    assert await admission.capture(RUN, MISSION, reference_keys=["evidence"])


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("case,reason", [
    ("missing", "original_evidence_missing"),
    ("conflict", "original_evidence_conflict"),
    ("wrong_run", "original_evidence_conflict"),
    ("duplicate", "evidence_store_integrity_failed"),
    ("read_error", "evidence_store_read_failed"),
    ("integrity_error", "evidence_store_read_failed"),
    ("no_integrity", "evidence_store_integrity_unavailable"),
    ("unsupported", "reference_update_proof_unsupported"),
    ("foreign_references", "evidence_reference_inventory_conflict"),
])
async def test_unproved_append_remains_fenced(tmp_path, monkeypatch, backend, case, reason):
    w = await world(tmp_path, backend)
    record = binding()
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    if case != "missing":
        stored = record.model_copy(update={"digest": "different"}) if case == "conflict" else (
            record.model_copy(update={"run_id": "different-run"}) if case == "wrong_run"
            else record)
        await base.append(stored)
    if case == "duplicate":
        await base.append(record)
    if case in {"read_error", "integrity_error"}:
        method = "get" if case == "read_error" else "verify_run_integrity"
        monkeypatch.setattr(base, method, AsyncMock(side_effect=OSError("private payload")))
    if case == "no_integrity":
        base = FakeEvidenceStore()
        await base.append(record)
    admission = PlanningAdmission(w.coordinator)
    refs = ["evidence", "evidence:binding", "validity:original"]
    if case == "foreign_references":
        refs.append("graph")
    expected = proof(record)
    if case == "unsupported":
        expected = expected.model_copy(update={"kind": "not-an-evidence-append"})
    await admission.begin_reference_update(RUN, refs, proof=expected)
    store = PlanningEvidenceStore(base, admission)
    async with w.coordinator.exclusive_run(RUN):
        (outcome,) = await store.reconcile_pending_writes(RUN)
    assert outcome.state == "held" and outcome.reason_code == reason
    assert "private payload" not in outcome.model_dump_json()
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["evidence"])


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_graph_and_legacy_fences_are_not_inferred_from_current_state(
    tmp_path, backend, legacy,
):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    writer = await admission.begin_reference_update(RUN, ["graph", "node:synthetic"])
    if legacy:
        async def remove_journal(tx):
            tx.records.pop(("planning_writer", f"{RUN}:{writer}"))
        await w.coordinator.change(remove_journal)
        if backend == "sqlite":
            # The production composite provider upserts append-only records;
            # simulate an old DB's actual absence, not a staged pop it ignores.
            with sqlite3.connect(w.db) as db:
                db.execute("DELETE FROM orchestration_records WHERE kind=? AND identity=?",
                           ("planning_writer", f"{RUN}:{writer}"))
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    async with w.coordinator.exclusive_run(RUN):
        (outcome,) = await PlanningEvidenceStore(base, admission).reconcile_pending_writes(RUN)
    assert outcome.state == "held" and outcome.reason_code == "reference_update_proof_missing"
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["graph"])


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("mutation", ["version", "writer", "proof", "extra_reference"])
async def test_change_during_original_read_cannot_clear_new_state(tmp_path, backend, mutation):
    w = await world(tmp_path, backend)
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    await pending_append(w, base)
    admission = PlanningAdmission(w.coordinator)
    (intent,) = await admission.pending_reference_updates(RUN)

    class ChangingStore:
        def __getattr__(self, name):
            return getattr(base, name)

        async def get(self, identity):
            async def mutate(tx):
                row = tx.records[("planning_reference", f"{RUN}:evidence")]
                if mutation == "version":
                    row["version"] += 1
                elif mutation == "writer":
                    row["writer"] = "new-owner"
                elif mutation == "proof":
                    saved = tx.records[("planning_writer", f"{RUN}:{intent.writer}")]
                    saved["proof"]["content_hash"] = "a" * 64
                else:
                    tx.records[("planning_reference", f"{RUN}:graph")] = {
                        "version": 1, "writer": intent.writer,
                    }
            await w.coordinator.change(mutate)
            return await base.get(identity)

    async with w.coordinator.exclusive_run(RUN):
        store = PlanningEvidenceStore(ChangingStore(), admission)
        (outcome,) = await store.reconcile_pending_writes(RUN)
    assert outcome.state == "held"
    assert outcome.reason_code == "reference_update_changed_during_recovery"
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["validity:original"])


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_finish_requires_complete_inventory_and_is_idempotent(tmp_path, backend):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    writer = await admission.begin_reference_update(RUN, ["a", "b"])
    with pytest.raises(StateConflict, match="inventory_mismatch"):
        await admission.finish_reference_update(RUN, ["a"], writer)
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["a", "b"])
    await admission.finish_reference_update(RUN, ["a", "b"], writer)
    new_writer = await admission.begin_reference_update(RUN, ["a", "b"])
    await admission.finish_reference_update(RUN, ["a", "b"], writer)  # cannot clear the new writer
    (pending,) = await admission.pending_reference_updates(RUN)
    assert pending.writer == new_writer
    assert pending.references == (("a", 2), ("b", 2))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_cancelled_recovery_retains_original_fence(tmp_path, backend):
    w = await world(tmp_path, backend)
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    await pending_append(w, base)

    class CancelRead:
        async def verify_run_integrity(self, run_id):
            raise asyncio.CancelledError

    admission = PlanningAdmission(w.coordinator)
    async with w.coordinator.exclusive_run(RUN):
        with pytest.raises(asyncio.CancelledError):
            await PlanningEvidenceStore(CancelRead(), admission).reconcile_pending_writes(RUN)
    assert len(await admission.pending_reference_updates(RUN)) == 1


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_other_run_and_unrelated_graph_fence_are_preserved(tmp_path, backend):
    w = await world(tmp_path, backend)
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    await pending_append(w, base)
    admission = PlanningAdmission(w.coordinator)
    await admission.begin_reference_update(RUN, ["graph"])
    other = await admission.begin_reference_update("other-run", ["evidence"])
    async with w.coordinator.exclusive_run(RUN):
        outcomes = await PlanningEvidenceStore(base, admission).reconcile_pending_writes(RUN)
    assert sorted(outcome.state for outcome in outcomes) == ["confirmed", "held"]
    assert await admission.capture_references(RUN, ["evidence"]) == (("evidence", 1),)
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.capture_references(RUN, ["graph"])
    (remaining,) = await admission.pending_reference_updates("other-run")
    assert remaining.writer == other


async def test_actual_process_exit_after_filesystem_append_recovers_same_sqlite_intent(tmp_path):
    w = await world(tmp_path, "sqlite")
    root = tmp_path / "evidence"
    script = """
import asyncio, os, sys
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.planning_evidence import PlanningEvidenceStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
class ExitAfterAppend(FilesystemEvidenceStore):
    async def append(self, record):
        await super().append(record)
        os._exit(0)
async def main():
    coordinator = coordinator_for(SQLiteTaskGraphStore(sys.argv[1]), SQLitePathStore(sys.argv[1]))
    store = PlanningEvidenceStore(ExitAfterAppend(sys.argv[2]), PlanningAdmission(coordinator))
    async with coordinator.exclusive_run("run-atomic"):
        await store.append(EvidenceRecord.model_validate_json(sys.argv[3]))
asyncio.run(main())
"""
    await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", script, str(w.db), str(root), binding().model_dump_json()],
        check=True, capture_output=True, timeout=15,
    )
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    coordinator = reopen(w, "sqlite")
    admission = PlanningAdmission(coordinator)
    (intent,) = await admission.pending_reference_updates(RUN)
    assert intent.proof is not None
    assert intent.proof.content_hash == content_hash(
        await FilesystemEvidenceStore(root).get("binding"),
    )
    async with coordinator.exclusive_run(RUN):
        (outcome,) = await PlanningEvidenceStore(
            FilesystemEvidenceStore(root), admission,
        ).reconcile_pending_writes(RUN)
    assert outcome.state == "confirmed" and outcome.writer == intent.writer
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_begin_fence_and_proof_roll_back_together_on_busy_reference(tmp_path, backend):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    writer = await admission.begin_reference_update(RUN, ["b"], proof=proof(binding()))
    with pytest.raises(StateConflict, match="update_pending"):
        await admission.begin_reference_update(RUN, ["a", "b"], proof=proof(binding()))
    assert await admission.capture_references(RUN, ["a"]) == (("a", 0),)
    (pending,) = await admission.pending_reference_updates(RUN)
    assert pending.writer == writer and pending.references == (("b", 1),)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("damage", ["missing_refs", "cleared_refs", "partial_refs", "wrong_index"])
async def test_orphan_write_journal_cannot_look_like_empty_inventory(tmp_path, backend, damage):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    writer = await admission.begin_reference_update(RUN, ["a", "b"], proof=proof(binding()))

    async def corrupt(tx):
        key = ("planning_writer", f"{RUN}:{writer}")
        if damage == "wrong_index":
            tx.records[("planning_writer", f"different-run:{writer}")] = tx.records.pop(key)
        else:
            for ref in ["a", "b"] if damage != "partial_refs" else ["a"]:
                reference = ("planning_reference", f"{RUN}:{ref}")
                if damage == "cleared_refs":
                    tx.records[reference]["writer"] = None
                else:
                    tx.records.pop(reference)

    await w.coordinator.change(corrupt)
    if backend == "sqlite" and damage in {"missing_refs", "partial_refs"}:
        with sqlite3.connect(w.db) as db:
            for ref in ["a", "b"] if damage == "missing_refs" else ["a"]:
                db.execute("DELETE FROM orchestration_records WHERE kind=? AND identity=?",
                           ("planning_reference", f"{RUN}:{ref}"))
    with pytest.raises(StateConflict, match="inventory_mismatch"):
        await admission.pending_reference_updates(RUN)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_boolean_version_is_not_a_matching_integer(tmp_path, backend):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    writer = await admission.begin_reference_update(RUN, ["a"], proof=proof(binding()))

    async def corrupt(tx):
        tx.records[("planning_reference", f"{RUN}:a")]["version"] = True

    await w.coordinator.change(corrupt)
    with pytest.raises(StateConflict, match="inventory_invalid"):
        await admission.finish_reference_update(RUN, ["a"], writer)
    with pytest.raises(StateConflict, match="inventory_invalid"):
        await admission.pending_reference_updates(RUN)


async def test_append_freezes_the_expected_original_before_external_await(tmp_path):
    w = await world(tmp_path, "memory")
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    started, release = asyncio.Event(), asyncio.Event()

    class SlowStore:
        def __getattr__(self, name):
            return getattr(base, name)

        async def append(self, record):
            started.set()
            await release.wait()
            await base.append(record)
            raise OSError("synthetic lost acknowledgement")

    admission = PlanningAdmission(w.coordinator)
    record = binding()
    expected = record.model_copy(deep=True)
    write = asyncio.create_task(PlanningEvidenceStore(SlowStore(), admission).append(record))
    await asyncio.wait_for(started.wait(), timeout=5)
    record.metadata["claim_evidence_id"] = "changed-by-caller"
    release.set()
    with pytest.raises(OSError):
        await write
    assert await base.get(record.evidence_id) == expected
    async with w.coordinator.exclusive_run(RUN):
        (outcome,) = await PlanningEvidenceStore(base, admission).reconcile_pending_writes(RUN)
    assert outcome.state == "confirmed"
