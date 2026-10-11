"""Read-only activation snapshots are diagnostics, never replay permission."""

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli import _build_parser
from redteam.app.cli.commands import status as command
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.task_activation import read_activation_diagnostics
from tests.unit.test_activation_identity import reservation
from tests.unit.test_orchestration_coordination import RUN


def snapshot(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


async def test_reserved_snapshot_preserves_all_records_and_does_not_grant_start(tmp_path):
    _, _, originals, path = await reservation(tmp_path)
    before = snapshot(path)
    result = await read_activation_diagnostics(str(path), RUN)
    assert result.run_id == RUN and len(result.activations) == 2
    assert {row.instance_id for row in result.activations} == {
        str(original.instance_id) for original in originals
    }
    assert all(row.reason_code == "reserved_proof_consistent" for row in result.activations)
    assert all(row.task_claim_matches for row in result.activations)
    assert result.replay_permitted is False
    assert result.external_stores == "not_inspected"
    assert all(sum(row.request_states.values()) == 0 for row in result.activations)
    assert snapshot(path) == before


@pytest.mark.parametrize("request_state", [
    "reserved", "dispatched", "settled", "unknown", "released",
])
async def test_entered_actor_reports_original_request_states_without_refund(
    tmp_path, request_state,
):
    _, activation, originals, path = await reservation(tmp_path)
    original = originals[0]
    await activation.entering(original)
    ledger = RunRequestLedger(activation.coordinator)
    request = await ledger.reserve(RUN, owner=original.work_owner, kind="native")
    if request_state == "released":
        await ledger.release_undispatched(RUN, request)
    elif request_state != "reserved":
        await ledger.dispatch(RUN, request)
        if request_state in {"settled", "unknown"}:
            await ledger.settle(RUN, request, unknown=request_state == "unknown")
    before = snapshot(path)
    result = await read_activation_diagnostics(str(path), RUN)
    row = next(item for item in result.activations if item.instance_id == str(original.instance_id))
    assert row.launch_stage == "entered"
    assert row.reason_code == "original_checkpoint_and_receipts_required"
    assert row.request_states[request_state] == 1
    peer = next(item for item in result.activations if item.instance_id != row.instance_id)
    assert sum(peer.request_states.values()) == 0
    assert result.request_states[request_state] == 1
    assert snapshot(path) == before


async def test_reserved_but_owner_activity_is_not_declared_safe(tmp_path):
    _, activation, originals, path = await reservation(tmp_path)
    ledger = RunRequestLedger(activation.coordinator)
    request = await ledger.reserve(RUN, owner=originals[0].work_owner, kind="native")
    await ledger.release_undispatched(RUN, request)
    result = await read_activation_diagnostics(str(path), RUN)
    row = next(item for item in result.activations
               if item.instance_id == str(originals[0].instance_id))
    assert row.reason_code == "activation_owner_activity_checkpoint_required"
    assert row.request_states["released"] == 1
    assert result.replay_permitted is False


async def test_entered_with_no_requests_is_not_a_never_executed_proof(tmp_path):
    _, activation, originals, path = await reservation(tmp_path)
    await activation.entering(originals[0])
    result = await read_activation_diagnostics(str(path), RUN)
    row = next(item for item in result.activations
               if item.instance_id == str(originals[0].instance_id))
    assert sum(row.request_states.values()) == 0
    assert row.reason_code == "original_checkpoint_and_receipts_required"
    assert result.replay_permitted is False


@pytest.mark.parametrize("failed", [False, True])
async def test_returned_and_held_keep_original_identity_without_settling_task(tmp_path, failed):
    w, activation, originals, path = await reservation(tmp_path)
    await activation.entering(originals[0])
    await activation.returned(originals[0], failed=failed)
    before = snapshot(path)
    result = await read_activation_diagnostics(str(path), RUN)
    row = next(item for item in result.activations
               if item.instance_id == str(originals[0].instance_id))
    assert row.state == ("held" if failed else "returned")
    assert row.reason_code == "original_checkpoint_and_receipts_required"
    assert (await w.tasks.get_task(originals[0].active_task_id)).occupant_owner == (
        originals[0].work_owner
    )
    assert snapshot(path) == before


async def test_cli_reads_without_report_model_transport_or_new_directories(
    tmp_path, monkeypatch, capsys,
):
    _, activation, originals, path = await reservation(tmp_path)
    await activation.entering(originals[0])
    before = snapshot(path)
    monkeypatch.setattr(command, "load_terminal_report", lambda *_: pytest.fail("not report mode"))
    from redteam.app import bootstrap
    build = AsyncMock(side_effect=AssertionError("diagnostics must not build"))
    monkeypatch.setattr(bootstrap, "build_application", build)
    args = _build_parser().parse_args(["status", RUN, "--activation-db", str(path), "--json"])
    assert await command.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["replay_permitted"] is False
    assert data["external_stores"] == "not_inspected"
    assert {row["launch_stage"] for row in data["activations"]} == {"reserved", "entered"}
    assert "runtime" not in data and "objective" not in json.dumps(data)
    assert snapshot(path) == before
    build.assert_not_awaited()


async def test_missing_and_legacy_store_are_unknown_without_creation(tmp_path, capsys):
    path = tmp_path / "absent-parent" / "absent.sqlite"
    args = _build_parser().parse_args(["status", RUN, "--activation-db", str(path), "--json"])
    assert await command.run(args) == 1
    assert not path.parent.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    with sqlite3.connect(tmp_path / "legacy.sqlite") as db:
        db.execute("CREATE TABLE legacy(identity TEXT)")
    assert await read_activation_diagnostics(str(tmp_path / "legacy.sqlite"), RUN) is None


@pytest.mark.parametrize("tamper", ["runtime", "hidden_run", "request_state", "future_schema"])
async def test_invalid_storage_is_not_safe_and_errors_never_dump_payload(tmp_path, tamper, capsys):
    _, activation, originals, path = await reservation(tmp_path)
    secret = "synthetic-secret-not-for-display"
    async def change(tx):
        row = tx.records[("task_activation", f"{RUN}:{originals[0].active_task_id}")]
        if tamper == "runtime":
            row["runtime"]["instance_id"] = {"password": secret}
        elif tamper == "hidden_run":
            row["run_id"] = secret
        elif tamper == "request_state":
            tx.records[("llm_request", "request-corrupt")] = {
                "request_id": "request-corrupt", "run_id": RUN,
                "owner": originals[0].work_owner, "state": secret,
            }
    await activation.coordinator.change(change)
    if tamper == "future_schema":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO orchestration_migrations VALUES(2)")
    before = snapshot(path)
    args = _build_parser().parse_args(["status", RUN, "--activation-db", str(path), "--json"])
    assert await command.run(args) == 1
    rendered = capsys.readouterr()
    assert not rendered.out and secret not in rendered.err
    assert snapshot(path) == before


async def test_changed_claim_and_legacy_stage_are_explicit_not_hidden(tmp_path):
    w, activation, originals, path = await reservation(tmp_path)
    task = await w.tasks.get_task(originals[0].active_task_id)
    await w.tasks.update_task(task.model_copy(update={"occupant_owner": "different-owner"}))
    async def change(tx):
        tx.records[("task_activation", f"{RUN}:{originals[1].active_task_id}")].pop(
            "launch_proof_version",
        )
    await activation.coordinator.change(change)
    result = await read_activation_diagnostics(str(path), RUN)
    by_id = {row.instance_id: row for row in result.activations}
    assert not by_id[str(originals[0].instance_id)].task_claim_matches
    assert by_id[str(originals[0].instance_id)].reason_code == "activation_task_proof_changed"
    assert by_id[str(originals[1].instance_id)].reason_code == "activation_checkpoint_required"


async def test_corrupt_identity_fails_entire_inventory_not_first_safe_row(tmp_path):
    _, activation, originals, path = await reservation(tmp_path)
    async def change(tx):
        tx.records[("task_activation", f"{RUN}:{originals[1].active_task_id}")]["instance_id"] = (
            str(originals[0].instance_id)
        )
    await activation.coordinator.change(change)
    with pytest.raises(StateConflict):
        await read_activation_diagnostics(str(path), RUN)
