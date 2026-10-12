"""Actual native-loop journals, read without redispatch or settlement.

The model/gateway are explicitly scripted. These are local storage/recovery
tests, not a live LLM or financial-target evaluation.
"""

import json
import sqlite3

import pytest

from redteam.app.cli import _build_parser
from redteam.app.cli.commands import status as command
from redteam.core.ids import RunId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.execution_diagnostics import inspect_execution_diagnostics
from redteam.orchestration.task_activation import read_activation_diagnostics
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.unit.test_activation_identity import reservation
from tests.unit.test_orchestration_coordination import RUN
from tests.unit.test_orchestration_work import item


def snapshot(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}










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
    monkeypatch.setattr(
        "redteam.app.report_store.load_terminal_report", lambda *_: pytest.fail("not report mode"),
    )
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
