"""Actual native-loop journals, read without redispatch or settlement.

The model/gateway are explicitly scripted. These are local storage/recovery
tests, not a live LLM or financial-target evaluation.
"""

import json
import sqlite3
from dataclasses import replace

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.app.cli import _build_parser
from redteam.app.cli.commands import status as command
from redteam.core.convert import digest_payload
from redteam.core.ids import EvidenceId, RunId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.execution_diagnostics import inspect_execution_diagnostics
from redteam.orchestration.execution_reconciliation import reconcile_execution_receipts
from redteam.orchestration.task_activation import read_activation_diagnostics
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.integration.test_execution_receipt_reconciliation import _world
from tests.integration.test_native_specialist_closed_loop import ClosedLoopProvider, assembled
from tests.unit.test_activation_identity import reservation
from tests.unit.test_orchestration_coordination import RUN
from tests.unit.test_orchestration_work import item


def snapshot(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


async def audit(tmp_path, runtime, works, evidence_dir):
    path = tmp_path / "work.sqlite"
    store = SQLiteWorkStore(path)
    for work in works:
        assert await store.add(work)
    before_db, before_files = snapshot(path), files(evidence_dir)
    observed = await inspect_execution_diagnostics(
        runtime.run_id, work_items=await SQLiteWorkStore.inspect_run(path, runtime.run_id),
        evidence_store=await FilesystemEvidenceStore.inspect_run(evidence_dir, runtime.run_id),
    )
    assert snapshot(path) == before_db and files(evidence_dir) == before_files
    assert observed.replay_permitted is False and observed.cross_store_atomic is False
    return observed


@pytest.mark.parametrize("case,expected", [
    ("publication_loss", "unprocessed"), ("non_entry", "no_entry"),
    ("late_original", "uncertain"), ("settled", "processed"),
    ("late_resolved", "processed"),
])
async def test_native_originals_are_classified_without_new_calls(tmp_path, case, expected):
    deps, gateway, store, runtime, work = await _world(
        tmp_path, late=case in {"late_original", "late_resolved"}, before=case == "non_entry",
    )
    if case in {"settled", "late_resolved"}:
        restored = await reconcile_execution_receipts(deps, runtime)
        assert restored.ready
        work = await deps.work_store.get(work.work_id)
    calls = len(gateway.invocations)
    queries, fetches = getattr(gateway, "queries", 0), getattr(gateway, "fetches", 0)
    observed = await audit(tmp_path, runtime, [work], tmp_path / "evidence")
    assert len(observed.executions) == 1
    row = observed.executions[0]
    assert row.state == expected and row.work_id == work.work_id
    if expected == "unprocessed":
        assert row.work_result_matches is False  # publication failure did not settle Work
    if expected == "processed":
        assert row.work_result_matches is True
    if expected == "uncertain":
        assert row.result_success is None and row.work_result_matches is None
    assert len(gateway.invocations) == calls
    assert getattr(gateway, "queries", 0) == queries and getattr(gateway, "fetches", 0) == fetches


async def test_submission_without_original_is_not_an_execution_failure_or_non_entry(tmp_path):
    _, _, store, runtime, work = await _world(tmp_path)
    missing = FilesystemEvidenceStore(tmp_path / "missing")
    for record in await store.list_for_run(runtime.run_id):
        if record.metadata.get("receipt_kind") == "execution_submission":
            await missing.append(record)
    observed = await audit(tmp_path, runtime, [work], tmp_path / "missing")
    assert observed.executions[0].state == "missing_original"
    assert observed.executions[0].result_success is None


@pytest.mark.parametrize("tamper,reason", [
    ("digest", "execution_receipt_digest_mismatch"),
    ("conflicting_result", "conflicting_execution_results"),
    ("missing_submission", "execution_submission_missing"),
    ("work_instance", "execution_recovery_work_mismatch"),
    ("work_task", "execution_recovery_work_mismatch"),
    ("processing", "execution_processing_receipt_invalid"),
])
async def test_invalid_original_or_work_correlation_is_not_hidden(tmp_path, tamper, reason):
    deps, _, store, runtime, work = await _world(tmp_path)
    if tamper == "processing":
        assert (await reconcile_execution_receipts(deps, runtime)).ready
        work = await deps.work_store.get(work.work_id)
    target = FilesystemEvidenceStore(tmp_path / "changed")
    for record in await store.list_for_run(runtime.run_id):
        kind = record.metadata.get("receipt_kind")
        if tamper == "missing_submission" and kind == "execution_submission":
            continue
        if tamper == "digest" and kind == "execution_result":
            record = record.model_copy(deep=True)
            record.metadata["original_result"]["output"] = {"password": "synthetic-secret"}
        if tamper == "processing" and kind == "execution_processed":
            record = record.model_copy(update={"digest": "not-the-original-digest"})
        await target.append(record)
        if tamper == "conflicting_result" and kind == "execution_result":
            conflict = record.model_copy(deep=True, update={"evidence_id": EvidenceId("conflict")})
            conflict.metadata["original_result"]["output"] = {"foreign": "synthetic body"}
            conflict.digest = digest_payload(conflict.metadata)
            await target.append(conflict)
    if tamper == "work_instance":
        work = work.model_copy(update={"instance_id": "foreign-instance"})
    if tamper == "work_task":
        work = work.model_copy(update={"mission_task_id": "foreign-task"})
    observed = await audit(tmp_path, runtime, [work], tmp_path / "changed")
    assert observed.executions[0].state == "invalid"
    assert observed.executions[0].reason_code == reason
    assert "synthetic-secret" not in observed.model_dump_json()


async def test_orphan_work_and_missing_stores_are_explicit_unknown(tmp_path):
    work = item("orphan", run_id="unknown-run").model_copy(update={
        "resource_keys": ["shared-resource"], "conflict_keys": ["shared-resource"],
        "resource_execution_pending": True,
    })
    observed = await inspect_execution_diagnostics(
        RunId("unknown-run"), work_items=[work], evidence_store=None,
    )
    assert observed.evidence_store == "unknown"
    assert observed.executions[0].state == "unknown"
    assert observed.executions[0].resource_execution_pending is True
    assert await SQLiteWorkStore.inspect_run(tmp_path / "absent/work.sqlite", work.run_id) is None
    assert await FilesystemEvidenceStore.inspect_run(
        tmp_path / "absent/evidence", work.run_id,
    ) is None
    assert not (tmp_path / "absent").exists()


async def test_orphan_work_in_known_empty_inventory_still_does_not_prove_non_entry(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "orphan-run.jsonl").write_text("", encoding="utf-8")
    work = item("orphan", run_id="orphan-run")
    observed = await inspect_execution_diagnostics(
        work.run_id, work_items=[work],
        evidence_store=await FilesystemEvidenceStore.inspect_run(root, work.run_id),
    )
    assert observed.evidence_store == "inspected"
    assert observed.executions[0].state == "unknown"
    assert observed.executions[0].reason_code == "work_without_execution_receipts"


async def test_missing_work_store_does_not_certify_known_original(tmp_path):
    _, _, store, runtime, _ = await _world(tmp_path)
    observed = await inspect_execution_diagnostics(
        runtime.run_id, work_items=None,
        evidence_store=await FilesystemEvidenceStore.inspect_run(
            tmp_path / "evidence", runtime.run_id,
        ),
    )
    assert observed.work_store == "unknown"
    assert observed.executions[0].state == "unknown"
    assert await store.list_for_run(runtime.run_id)  # originals retained


async def test_evidence_snapshot_is_detached_and_refuses_append(tmp_path):
    _, _, _, runtime, _ = await _world(tmp_path)
    view = await FilesystemEvidenceStore.inspect_run(tmp_path / "evidence", runtime.run_id)
    original = (await view.list_for_run(runtime.run_id))[0]
    clone = await view.get(original.evidence_id)
    clone.metadata.clear()
    assert (await view.get(original.evidence_id)).metadata == original.metadata
    with pytest.raises(RuntimeError, match="read_only"):
        await view.append(original)


@pytest.mark.parametrize("tamper", ["index", "unindexed", "duplicate_index", "truncated"])
async def test_evidence_inventory_integrity_failure_is_not_empty(tmp_path, tamper):
    _, _, _, runtime, _ = await _world(tmp_path)
    root = tmp_path / "evidence"
    path, index = root / f"{runtime.run_id}.jsonl", root / "_index.jsonl"
    if tamper == "index":
        index.write_text("", encoding="utf-8")
    elif tamper == "unindexed":
        index.unlink()
    elif tamper == "duplicate_index":
        index.write_text(index.read_text() * 2, encoding="utf-8")
    else:
        path.write_text(path.read_text()[:-17], encoding="utf-8")
    before = files(root)
    with pytest.raises(ValueError):
        await FilesystemEvidenceStore.inspect_run(root, runtime.run_id)
    assert files(root) == before


@pytest.mark.parametrize("column,value", [
    ("run_id", "foreign-run"), ("instance_id", "foreign-instance"), ("status", "succeeded"),
])
async def test_work_hidden_indexes_fail_before_run_filter(tmp_path, column, value):
    path = tmp_path / "work.sqlite"
    store = SQLiteWorkStore(path)
    await store.add(item("work", run_id=RUN))
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE work_items SET {column}=?", (value,))
    before = snapshot(path)
    with pytest.raises(ValueError, match="index_mismatch"):
        await SQLiteWorkStore.inspect_run(path, RunId(RUN))
    assert snapshot(path) == before


async def test_legacy_work_schema_is_not_silently_migrated(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE work_items(work_id TEXT, payload TEXT)")
    before = snapshot(path)
    with pytest.raises(ValueError, match="schema_unsupported"):
        await SQLiteWorkStore.inspect_run(path, RunId(RUN))
    assert snapshot(path) == before


async def test_migrated_but_unattributed_legacy_work_is_not_hidden(tmp_path):
    path = tmp_path / "work.sqlite"
    store = SQLiteWorkStore(path)
    await store.add(item("legacy").model_copy(update={"run_id": None}))
    before = snapshot(path)
    with pytest.raises(ValueError, match="unattributed"):
        await SQLiteWorkStore.inspect_run(path, RunId(RUN))
    assert snapshot(path) == before


@pytest.mark.parametrize("run_id", ["../other", "a/b", "a\\b", "", ".."])
async def test_evidence_run_path_cannot_escape_selected_root(tmp_path, run_id):
    with pytest.raises(ValueError, match="run_id_invalid"):
        await FilesystemEvidenceStore.inspect_run(tmp_path / "absent", RunId(run_id))
    assert not (tmp_path / "absent").exists()


async def test_cli_explicit_cross_store_read_keeps_canonical_state(tmp_path, monkeypatch, capsys):
    _, activation, originals, path = await reservation(tmp_path)
    await activation.entering(originals[0])
    work_path = tmp_path / "work.sqlite"
    store = SQLiteWorkStore(work_path)
    await store.add(item("orphan", run_id=RUN).model_copy(update={
        "instance_id": originals[0].instance_id,
    }))
    before = snapshot(path), snapshot(work_path)
    monkeypatch.setattr(command, "load_terminal_report", lambda *_: pytest.fail("not report mode"))
    args = _build_parser().parse_args([
        "status", RUN, "--activation-db", str(path), "--work-db", str(work_path),
        "--evidence-dir", str(tmp_path / "absent"), "--json",
    ])
    assert await command.run(args) == 0  # read succeeded, NOT recovery authorized
    observed = json.loads(capsys.readouterr().out)
    assert observed["external_stores"] == "independently_inspected"
    assert observed["schema_version"] == 2
    assert observed["activation"]["schema_version"] == 1
    assert observed["activation_projection_changed"] is False
    assert observed["execution"]["replay_permitted"] is False
    assert observed["execution"]["evidence_store"] == "unknown"
    assert observed["execution"]["executions"][0]["state"] == "unknown"
    assert not (tmp_path / "absent").exists()
    assert before == (snapshot(path), snapshot(work_path))


async def test_cli_reports_concurrent_canonical_change_without_overwriting_it(
    tmp_path, monkeypatch, capsys,
):
    _, activation, originals, path = await reservation(tmp_path)
    original_inspect = SQLiteWorkStore.inspect_run
    async def inspect_and_change(work_path, run_id):
        result = await original_inspect(work_path, run_id)
        await activation.entering(originals[0])
        return result
    monkeypatch.setattr(SQLiteWorkStore, "inspect_run", inspect_and_change)
    args = _build_parser().parse_args([
        "status", RUN, "--activation-db", str(path), "--work-db", str(tmp_path / "absent.sqlite"),
        "--evidence-dir", str(tmp_path / "absent"), "--json",
    ])
    assert await command.run(args) == 0
    assert json.loads(capsys.readouterr().out)["activation_projection_changed"] is True
    after = await read_activation_diagnostics(str(path), RUN)
    assert any(row.launch_stage == "entered" for row in after.activations)


async def test_cli_storage_error_is_not_absence_and_never_prints_raw_content(tmp_path, capsys):
    _, _, _, path = await reservation(tmp_path)
    root = tmp_path / "broken-evidence"
    root.mkdir()
    secret = "synthetic-password-do-not-print"
    (root / "_index.jsonl").write_text(secret, encoding="utf-8")
    args = _build_parser().parse_args([
        "status", RUN, "--activation-db", str(path), "--work-db", str(tmp_path / "absent.sqlite"),
        "--evidence-dir", str(root), "--json",
    ])
    before = snapshot(path), files(root)
    assert await command.run(args) == 1
    rendered = capsys.readouterr()
    assert not rendered.out and "JSONDecodeError" in rendered.err and secret not in rendered.err
    assert before == (snapshot(path), files(root))


@pytest.mark.parametrize("flags", [
    ["--work-db", "absent"], ["--evidence-dir", "absent"],
    ["--work-db", "absent", "--evidence-dir", "absent"],
    ["--activation-db", "absent", "--work-db", "absent"],
])
async def test_partial_explicit_cli_paths_are_config_errors(flags, capsys):
    assert await command.run(_build_parser().parse_args(["status", RUN, *flags])) == 2
    assert not capsys.readouterr().out


async def test_public_specialist_run_and_cli_match_actual_work_task_and_originals(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.chdir(tmp_path)
    provider = ClosedLoopProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider, durable=True)
    deps = replace(deps, evidence_store=FilesystemEvidenceStore(tmp_path / "evidence"))
    final = await run_engagement(engagement, deps)
    assert len(gateway.invocations) == 2
    calls = len(provider.requests)
    state_path, work_path = tmp_path / "task-graph.sqlite3", tmp_path / "work.sqlite3"
    before = snapshot(state_path), snapshot(work_path), files(tmp_path / "evidence")
    args = _build_parser().parse_args([
        "status", str(final.run_id), "--activation-db", str(state_path),
        "--work-db", str(work_path), "--evidence-dir", str(tmp_path / "evidence"), "--json",
    ])
    assert await command.run(args) == 0
    observed = json.loads(capsys.readouterr().out)
    rows = observed["execution"]["executions"]
    assert len(rows) == 2
    assert all(row["state"] == "processed" and row["activation_matches"]
               and row["work_result_matches"] for row in rows)
    assert observed["execution"]["replay_permitted"] is False
    assert len(provider.requests) == calls and len(gateway.invocations) == 2
    assert before == (snapshot(state_path), snapshot(work_path), files(tmp_path / "evidence"))
