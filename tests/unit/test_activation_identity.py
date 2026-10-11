"""Read-only bootstrap hints are distinct from replay/dispatch permission."""

import json
import sqlite3

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.component_binding import builtin_component_binding
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.state_backend import inspect_sqlite_state
from redteam.orchestration.task_activation import read_activation_identity
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.integration.test_ready_task_activation import prepared
from tests.unit.test_orchestration_coordination import RUN


async def reservation(tmp_path):
    w, activation, base = await prepared(tmp_path, "sqlite")
    policy = LLMRequestPolicy(mode="bounded", max_requests=4)
    await RunRequestLedger(activation.coordinator).bind(RUN, policy)
    async def bind(tx):
        tx.records[("planning_inputs", RUN)] = "synthetic-assembler-hash"
    await activation.coordinator.change(bind)
    base = base.model_copy(update={"component_binding": builtin_component_binding()})
    originals = await activation.ready(base, max_instances=2, max_activations=2)
    return w, activation, originals, tmp_path / "coordination.sqlite"


async def test_absent_file_and_legacy_store_never_created_or_migrated(tmp_path):
    missing = tmp_path / "not-created" / "state.sqlite"
    assert await read_activation_identity(str(missing), RUN) is None
    assert not missing.parent.exists()
    legacy = tmp_path / "legacy.sqlite"
    await SQLiteTaskGraphStore(legacy).setup()
    with sqlite3.connect(legacy) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    assert await read_activation_identity(str(legacy), RUN) is None
    with sqlite3.connect(legacy) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before


async def test_detached_inspection_preserves_claim_records_policy_and_schema(tmp_path):
    w, activation, originals, path = await reservation(tmp_path)
    with sqlite3.connect(path) as db:
        before = db.iterdump()
        before = tuple(before)
    hint = await read_activation_identity(str(path), RUN)
    assert hint.engagement_id == originals[0].engagement_id
    assert hint.mission_id == originals[0].mission_id
    assert hint.llm_request_policy == LLMRequestPolicy.model_validate(
        originals[0].llm_request_policy,
    )
    async def detached(tx):
        assert tx.paths.peek_active_pointer(RUN) is None
        tx.records[("binding", RUN)]["closed"] = True
        return True
    assert await inspect_sqlite_state(path, detached) is True
    assert await read_activation_identity(str(path), RUN) == hint
    with sqlite3.connect(path) as db:
        assert tuple(db.iterdump()) == before
    for original in originals:
        task = await w.tasks.get_task(original.active_task_id)
        assert task.occupant_owner == original.work_owner
    assert await activation.unstarted(RUN, checkpoint_instances=set()) == originals


@pytest.mark.parametrize("change", ["future", "version_missing", "partial_schema", "bad_json"])
async def test_unreadable_or_future_storage_is_not_absence(tmp_path, change):
    _, _, _, path = await reservation(tmp_path)
    with sqlite3.connect(path) as db:
        if change == "future":
            db.execute("INSERT INTO orchestration_migrations VALUES(2)")
        elif change == "version_missing":
            db.execute("DELETE FROM orchestration_migrations")
        elif change == "partial_schema":
            db.execute("DROP TABLE orchestration_migrations")
        else:
            db.execute("UPDATE orchestration_records SET payload='invalid' WHERE kind='binding'")
    with pytest.raises((CoordinationError, sqlite3.DatabaseError, json.JSONDecodeError)):
        await read_activation_identity(str(path), RUN)


@pytest.mark.parametrize("change", ["entered_peer", "mixed_components", "missing_components",
                                         "hidden_run", "wrong_key", "malformed_row", "duplicate",
                                         "old_inputs", "owner_request"])
async def test_no_filter_to_only_the_first_safe_reservation(tmp_path, change):
    _, activation, originals, path = await reservation(tmp_path)
    original = originals[1]
    async def mutate(tx):
        key = ("task_activation", f"{RUN}:{original.active_task_id}")
        row = tx.records[key]
        if change == "entered_peer":
            row["launch_stage"] = "entered"
        elif change in {"mixed_components", "missing_components"}:
            row["runtime"]["component_binding"] = (
                None if change == "missing_components" else builtin_component_binding().model_copy(
                    update={"specialist_fingerprint": "different"},
                ).model_dump(mode="json")
            )
            row["runtime_digest"] = content_hash(row["runtime"])
        elif change == "hidden_run":
            row["run_id"] = "unrelated-run"
        elif change == "wrong_key":
            tx.records[("task_activation", "wrong-index")] = tx.records.pop(key)
        elif change == "malformed_row":
            tx.records[key] = None
        elif change == "duplicate":
            row["instance_id"] = str(originals[0].instance_id)
        elif change == "old_inputs":
            row["input_binding_hash"] = None
    await activation.coordinator.change(mutate)
    if change == "owner_request":
        ledger = RunRequestLedger(activation.coordinator)
        request = await ledger.reserve(RUN, owner=original.work_owner, kind="synthetic")
        await ledger.release_undispatched(RUN, request)
    with pytest.raises(StateConflict):
        await read_activation_identity(str(path), RUN)
