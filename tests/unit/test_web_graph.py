"""Graph read model over a real central state: stored originals only, read-only access."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from redteam.app.web.graph_read import read_graph
from redteam.attack_graph.hypotheses import prepare_hypothesis_update
from redteam.core.contracts.execution import (
    ExecutionOutcomeProjection,
    ExecutionSelectionProjection,
    ExecutionState,
)
from redteam.core.contracts.graph import CandidateHypothesis, HypothesisUpdate
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import LLMProviderFailure
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
    assert view["planner"]["state"] == "held"
    assert view["planner"]["last_judgment"] == {
        "termination": "held", "reason": "no useful question left",
    }
    assert {key: value for key, value in view["planner"]["usage"].items() if key != "usage"} == {
        "requests": 1, "in_flight": 0, "known_tokens": 42, "unknown_usage": 0,
        "unknown_outcome": 0,
    }
    assert view["planner"]["usage"]["usage"]["cache_read_tokens"] == {"known": 0, "unknown": 1}


async def test_split_planner_live_stage_and_usage_are_read_from_original_requests(tmp_path):
    state, ledger = await bound(tmp_path)
    for stage in ("hypothesis_planning", "task_atomization"):
        request = await ledger.reserve(RUN, owner="hypothesis_planning",
                                       kind="complete_structured", stage=stage)
        view = await graph(state)
        assert view["planner"]["state"] == "deciding"
        assert view["planner"]["stage"] == stage
        await ledger.dispatch(RUN, request)
        await ledger.settle(RUN, request)
    view = await graph(state)
    assert view["planner"]["usage"]["requests"] == 2
    assert view["usage"]["by_stage"]["hypothesis_planning"]["unknown_usage"] == 1
    assert view["usage"]["by_stage"]["task_atomization"]["spent"] == 1
    assert view["usage"]["requests"] == sum(
        item["spent"] for item in view["usage"]["by_stage"].values()
    )
    review = await ledger.reserve(RUN, owner="hypothesis_planning", kind="complete_structured",
                                  stage="task_completion_review")
    view = await graph(state)
    assert view["planner"]["state"] == "waiting" and view["planner"]["usage"]["requests"] == 2
    await ledger.release_undispatched(RUN, review)


async def test_candidate_is_projected_separately_from_tasks_findings_and_mission(tmp_path):
    state, _ = await bound(tmp_path)
    candidate = CandidateHypothesis(
        hypothesis_id="candidate-a",
        description="Authorization may be missing",
        status="open",
        contract_version=2,
        target_node_ids=["host:example.invalid"],
        reason="Verify one response",
    )
    node = prepare_hypothesis_update(
        None,
        HypothesisUpdate(hypothesis=candidate),
        now=datetime.now(timezone.utc),
        run_id=RUN,
    )
    node = node.model_copy(update={"id": f"{state.engagement_id}\x1f{node.id}"})
    await state.transaction(lambda tx: tx.db.execute(
        "INSERT INTO state_graph_nodes VALUES(?,?,?,?)",
        (state.engagement_id, str(node.id), node.type.value, node.model_dump_json()),
    ))

    view = await graph(state)
    assert view["candidates"] == [{
        "hypothesis_id": "candidate-a",
        "description": "Authorization may be missing",
        "status": "open",
        "confidence": 0.0,
        "revision": 1,
        "reason": "Verify one response",
        "target_node_ids": ["host:example.invalid"],
        "supporting_evidence_ids": [],
        "refuting_evidence_ids": [],
        "source_hypothesis_ids": [],
        "task_ids": [],
    }]
    assert view["origin"]["hypotheses"] == 1
    assert {"from": "planner", "to": "candidate:candidate-a", "kind": "judged"} in view[
        "edges"
    ]
    assert view["tasks"] == [] and view["origin"]["findings"] == 0


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
        selection=ExecutionSelectionProjection(
            route="target", route_status="target_ready",
            route_reason_code="target_route_configured", selected_program="curl",
            program_executable="/usr/bin/curl",
        ),
        outcome=ExecutionOutcomeProjection(
            result_kind="not_executed", diagnostic_category="approval_pending",
            diagnostic_code="approval_pending", cause_scope="policy_wait",
            resume_condition="Approve the original fingerprint.",
        ),
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
    projected, = b["executions"]
    assert projected["route"] == "target" and projected["route_status"] == "target_ready"
    assert projected["selected_program"] == "curl"
    assert projected["result_kind"] == "not_executed"
    assert projected["diagnostic_category"] == "approval_pending"
    assert projected["resume_condition"] == "Approve the original fingerprint."
    assert next(item for item in view["executions"]
                if item["invocation_id"] == "invocation-b") == projected
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
    request = await ledger.reserve(
        RUN, owner="worker-a", kind="complete_native", stage="native_action",
        purpose="worker_decision",
    )
    await ledger.dispatch(RUN, request)
    during = await graph(state)
    assert by_instance(during, "instance-a")["phase"] == "model"
    assert during["llm"]["waiting"] == 1
    assert during["llm"]["exchanges"][0]["response_state"] == "waiting"
    assert {"from": "worker:instance-a", "to": "llm", "kind": "model_exchange"} in (
        during["edges"]
    )
    await ledger.settle_provider_outcome(RUN, request, total_tokens=5)
    await ledger.classify_decision(
        RUN, request, purpose="worker_external_tool_decision", outcome="accepted",
        valid=True, made_progress=True,
    )
    exchange = (await graph(state))["llm"]["exchanges"][0]
    assert exchange == {
        "request_id": request,
        "sequence": 1,
        "actor": "worker:instance-a",
        "owner": "worker-a",
        "task_id": None,
        "stage": "native_action",
        "kind": "complete_native",
        "purpose": "worker_external_tool_decision",
        "attempt_kind": "normal",
        "request_state": "settled",
        "response_state": "received",
        "provider_outcome": "success",
        "provider_status": None,
        "provider_code": None,
        "provider_retryable": None,
        "provider_retry_after": None,
        "decision_outcome": "accepted",
        "decision_valid": True,
        "made_progress": True,
        "request_content": None,
        "request_truncated": None,
        "response_content": None,
        "response_truncated": None,
        "tokens": {
            "input": None, "output": None, "cache_read": None,
            "cache_write": None, "total": 5,
        },
    }

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


async def test_provider_failure_details_and_interrupted_requests_are_distinct(tmp_path):
    state, ledger = await bound(tmp_path)
    await owner(state)
    failed = await ledger.reserve(
        RUN, owner="worker-a", kind="complete_native", stage="native_action",
        purpose="worker_decision",
    )
    await ledger.dispatch(RUN, failed)
    await ledger.settle_provider_outcome(
        RUN,
        failed,
        failure=LLMProviderFailure(
            kind="server",
            http_status=503,
            provider_code="service_unavailable",
            retryable=True,
            retry_after=1.5,
        ),
    )
    interrupted = await ledger.reserve(
        RUN, owner="worker-a", kind="complete_native", stage="native_action",
        purpose="worker_decision", attempt_kind="transport_retry",
    )
    await ledger.dispatch(RUN, interrupted)
    assert await ledger.settle_interrupted_dispatch(RUN, interrupted) is True

    view = await graph(state)
    exchanges = {item["request_id"]: item for item in view["llm"]["exchanges"]}
    assert view["llm"]["failure_breakdown"] == {"server": 1}
    assert view["llm"]["failures"] == 1
    assert view["llm"]["unknown"] == 1
    assert exchanges[failed]["response_state"] == "failed"
    assert {
        key: exchanges[failed][key]
        for key in (
            "provider_outcome", "provider_status", "provider_code",
            "provider_retryable", "provider_retry_after",
        )
    } == {
        "provider_outcome": "server",
        "provider_status": 503,
        "provider_code": "service_unavailable",
        "provider_retryable": True,
        "provider_retry_after": 1.5,
    }
    assert exchanges[interrupted]["response_state"] == "unknown"
    assert exchanges[interrupted]["provider_outcome"] is None
    assert exchanges[interrupted]["provider_status"] is None
    assert exchanges[interrupted]["provider_retryable"] is None


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
    state, ledger = await bound(tmp_path)
    await owner(state)
    request_id = await ledger.reserve(
        RUN, owner="worker-a", kind="complete_native",
    )

    def inject_private_bodies(tx):
        row = tx.db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? AND kind='llm_request' "
            "AND identity=?", (RUN, request_id),
        ).fetchone()
        payload = json.loads(row[0])
        payload.update(prompt="api_key=sk-prompt-secret-123456",
                       response="api_key=sk-response-secret-123456")
        tx.db.execute(
            "UPDATE state_requests SET payload=? WHERE run_id=? AND kind='llm_request' "
            "AND identity=?", (json.dumps(payload), RUN, request_id),
        )

    await state.transaction(inject_private_bodies)
    await state.transaction(lambda tx: tx.db.execute(
        "INSERT INTO state_requests VALUES(?,'planner_termination',?,?)",
        (RUN, RUN, json.dumps({"termination": "held",
                               "reason": "api_key=sk-test-secret-value-123456 leaked"})),
    ))
    text = json.dumps(await graph(state))
    assert "sk-test-secret-value-123456" not in text
    assert "sk-prompt-secret-123456" not in text
    assert "sk-response-secret-123456" not in text


async def test_redacted_public_prompt_and_response_appear_in_the_projection(tmp_path):
    state, ledger = await bound(tmp_path)
    await owner(state)
    request_id = await ledger.reserve(
        RUN,
        owner="worker-a",
        kind="complete_native",
        request_payload={
            "system": "Inspect the captured response",
            "messages": [{
                "role": "user",
                "content": "authorization: Bearer secret-token-value",
            }],
        },
    )
    await ledger.dispatch(RUN, request_id)
    await ledger.settle_provider_outcome(
        RUN,
        request_id,
        response_payload={
            "text": "A public observation is available",
            "password": "response-secret-value",
        },
    )

    exchange = (await graph(state))["llm"]["exchanges"][0]
    assert "Inspect the captured response" in exchange["request_content"]
    assert "A public observation is available" in exchange["response_content"]
    assert exchange["request_truncated"] is False
    assert exchange["response_truncated"] is False
    serialized = json.dumps(exchange)
    assert "secret-token-value" not in serialized
    assert "response-secret-value" not in serialized


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


def test_candidate_planner_input_carries_operator_guidance_without_view_guessing():
    import inspect

    from redteam.app.web.graph_model import build_graph  # noqa: F401
    from redteam.orchestration.hypothesis_planner import (
        _advance_hypotheses,
        advance_hypotheses,
    )

    wrapper_source = inspect.getsource(advance_hypotheses)
    planner_source = inspect.getsource(_advance_hypotheses)
    assert "return await _advance_hypotheses(planner, runtime, schema)" in wrapper_source
    assert '"operator_instructions": context["operator_instructions"]' in planner_source
