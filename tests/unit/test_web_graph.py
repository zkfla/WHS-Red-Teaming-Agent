"""Graph read model over a real central state: stored originals only, read-only access."""

from __future__ import annotations

import json
import sqlite3

import pytest

from redteam.app.web.graph_read import read_graph
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.tool_binding import execution_fingerprint
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from tests.unit.test_central_state import RUN, execution, original, owner, world


async def bound(tmp_path):
    state = await world(tmp_path)
    ledger = state.request_ledger()
    await ledger.bind(
        RUN, LLMRequestPolicy(mode="bounded", max_requests=20, max_concurrent_requests=4)
    )
    return state, ledger


async def graph(state):
    return await read_graph(state.path, RUN)


def by_instance(view, instance):
    return next(w for w in view["workers"] if w["instance_id"] == instance)


async def test_planner_state_follows_the_stored_model_request_and_judgment(tmp_path):
    state, ledger = await bound(tmp_path)
    view = await graph(state)
    assert view["available"] and view["planner"]["state"] == "waiting"
    assert view["planner"]["stage"] is None and view["planner"]["trigger"] is None
    assert view["planner"]["inputs"]["open_questions"] is None  # not stored: never guessed

    request = await ledger.reserve(RUN, owner="specialist_planning", kind="complete_structured")
    assert (await graph(state))["planner"]["state"] == "deciding"
    await ledger.dispatch(RUN, request)
    assert (await graph(state))["planner"]["usage"]["in_flight"] == 1
    await ledger.settle(RUN, request, total_tokens=42)
    await state.transaction(lambda tx: tx.db.execute(
        "INSERT INTO state_requests VALUES(?,'planner_termination',?,?)",
        (RUN, RUN, json.dumps({"termination": "held", "reason": "no useful question left"})),
    ))
    view = await graph(state)
    assert view["planner"]["state"] == "waiting"
    assert view["planner"]["last_judgment"] == {
        "termination": "held", "reason": "no useful question left",
    }
    assert view["planner"]["usage"] == {
        "requests": 1, "in_flight": 0, "known_tokens": 42, "unknown_usage": 0,
        "unknown_outcome": 0,
    }


async def test_two_workers_one_running_a_tool_and_one_waiting_for_approval_with_slot_returned(
    tmp_path,
):
    state, _ = await bound(tmp_path)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    await owner(state, "task-a", "worker-a", "instance-a")
    await owner(state, "task-b", "worker-b", "instance-b")
    proposal_a = await original(evidence, "proposal-a", action="action-a",
                                invocation="invocation-a", task_id="task-a",
                                instance_id="instance-a")
    proposal_b = await original(evidence, "proposal-b", action="action-b",
                                invocation="invocation-b", task_id="task-b",
                                instance_id="instance-b")
    running = execution(proposal_evidence_id=proposal_a[0].record.evidence_id,
                        resource_keys=["session-a"])
    await state.transaction(lambda tx: tx.record_execution(
        running, owner="worker-a", checked=proposal_a, now=100))
    await state.transaction(lambda tx: tx.update_execution(
        running.model_copy(update={"state": ExecutionState.RUNNING}), owner="worker-a", now=100))
    waiting = execution(
        task_id="task-b", instance_id="instance-b", action_id="action-b",
        invocation_id="invocation-b", model_tool_call_id="call-b",
        proposal_evidence_id=proposal_b[0].record.evidence_id, resource_keys=["session-b"],
        wait_reason="approval", approval_ref="approval-b",
        approval_fingerprint=execution_fingerprint("tool-any", None, {}),
    )
    await state.transaction(lambda tx: tx.record_execution(
        waiting, owner="worker-b", checked=proposal_b, now=100))
    await state.transaction(lambda tx: tx.actor_returned(
        "task-b", owner="worker-b", version=1, instance_id="instance-b"))

    view = await graph(state)
    a, b = by_instance(view, "instance-a"), by_instance(view, "instance-b")
    assert (a["phase"], a["current_tool"], a["slot_released"]) == ("tool", "tool-any", False)
    assert (b["phase"], b["slot_released"]) == ("waiting_approval", True)
    assert b["wait"] == {"reason": "approval", "approval_ref": "approval-b"}
    tasks = {t["task_id"]: t for t in view["tasks"]}
    assert tasks["task-a"]["worker"] == "instance-a"
    assert tasks["task-b"]["worker"] is None  # slot returned, task still held by its execution
    assert tasks["task-b"]["wait"]["reason"] == "approval"
    assert {"from": "planner", "to": "task:task-a", "kind": "registered"} in view["edges"]
    assert {"from": "task:task-a", "to": "worker:instance-a", "kind": "held_by"} in view["edges"]
    assert {"from": "task:task-b", "to": "worker:instance-b", "kind": "returned"} in view["edges"]


async def test_model_phase_and_unknown_outcome_are_never_shown_as_success(tmp_path):
    state, ledger = await bound(tmp_path)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    await owner(state)
    request = await ledger.reserve(RUN, owner="worker-a", kind="complete_native")
    await ledger.dispatch(RUN, request)
    assert by_instance(await graph(state), "instance-a")["phase"] == "model"
    await ledger.settle(RUN, request, total_tokens=5)

    proposal = await original(evidence, "proposal-a")
    intent = execution(proposal_evidence_id=proposal[0].record.evidence_id)
    await state.transaction(lambda tx: tx.record_execution(
        intent, owner="worker-a", checked=proposal, now=100))
    entered = await state.transaction(lambda tx: tx.update_execution(
        intent.model_copy(update={"state": ExecutionState.RUNNING}), owner="worker-a", now=100))
    await state.transaction(lambda tx: tx.update_execution(
        entered.model_copy(update={"state": ExecutionState.CANCELLED}), owner="worker-a", now=500))
    worker = by_instance(await graph(state), "instance-a")
    assert worker["phase"] == "outcome_unknown"
    assert worker["tool_states"] == {"unknown": 1}
    assert worker["model_requests"] == 1


async def test_read_is_read_only_and_never_creates_or_changes_a_database(tmp_path):
    state, _ = await bound(tmp_path)
    await owner(state)
    before = state.path.read_bytes()
    await graph(state)
    assert state.path.read_bytes() == before

    missing = tmp_path / "missing" / "state.sqlite3"
    assert (await read_graph(missing, RUN))["available"] is False
    assert not missing.parent.exists()
    other = await read_graph(state.path, "run-not-there")
    assert other == {"available": False, "reason": "central_original_run_missing",
                     "run_id": "run-not-there"}

    plain = tmp_path / "plain.db"
    sqlite3.connect(plain).execute("CREATE TABLE x(a)").connection.close()
    assert (await read_graph(plain, RUN))["available"] is False


async def test_secret_material_never_appears_in_the_projection(tmp_path):
    state, _ = await bound(tmp_path)
    await owner(state)
    await state.transaction(lambda tx: tx.db.execute(
        "INSERT INTO state_requests VALUES(?,'planner_termination',?,?)",
        (RUN, RUN, json.dumps({"termination": "held",
                               "reason": "api_key=sk-test-secret-value-123456 leaked"})),
    ))
    text = json.dumps(await graph(state))
    assert "sk-test-secret-value-123456" not in text


@pytest.mark.parametrize("bad", ["../x", "a b", "x" * 200])
async def test_api_rejects_malformed_run_ids(bad, tmp_path):
    import httpx

    from redteam.app.web.host import SessionHost
    from redteam.app.web.security import GuardPolicy
    from redteam.app.web.server import create_app
    from redteam.app.web.settings import SettingsStore

    settings = SettingsStore(env={}, state_path=tmp_path / "w.json", profile_dir=tmp_path,
                             env_path=tmp_path / ".env")
    host = SessionHost(settings=settings, launcher=object())
    app = create_app(host, GuardPolicy.for_loopback(8787))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
        response = await client.get("/api/v1/graph", params={"run_id": bad})
        assert response.status_code == 400
        ok = await client.get("/api/v1/graph")
        assert ok.json() == {"available": False, "reason": "state_unconfigured", "run_id": None}


def test_planner_input_does_not_carry_operator_guidance_so_the_view_says_so():
    import inspect

    from redteam.app.web.graph_model import build_graph  # noqa: F401
    from redteam.orchestration.specialist_planner import SpecialistPlanner

    source = inspect.getsource(SpecialistPlanner._advance_central)
    # If guidance is ever added to the planner payload, graph_model must report it.
    assert "operator_instructions" not in source
