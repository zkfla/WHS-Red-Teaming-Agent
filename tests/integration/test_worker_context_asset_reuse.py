"""Native probe worker chooses the advertised context.query through the real tool loop.

Explicit scripted provider and doubles only: no MCP, target, network or live model call.
The worker reads its own Task and forward source ancestry, then reuses a returned ref in a
follow-up. A sibling Task that shares its target, or a candidate revision changed after the
worker was admitted, is refused by the existing reader.
"""

from __future__ import annotations

import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import result_for
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import CONTEXT_QUERY_TOOL_ID
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import drive, selection

CANDIDATE = "candidate"
CONTEXT_TOOL = exposed_tool_name(CONTEXT_QUERY_TOOL_ID)


def _update(revision, description):
    return {"expected_revision": revision, "hypothesis": {
        "contract_version": 2, "hypothesis_id": CANDIDATE, "description": description,
        "target_node_ids": [TARGET], "reason": "Verify the observed response",
        "status": "open", "supporting_evidence_ids": [],
    }}


async def _register(session, key, *, source=None, source_result=None, hypotheses=None,
                    updates=(), probe=True):
    state = session.deps.central_state
    service = AttackGraphService(session.deps.graph_store)
    mission = await state.get_mission(session.runtime.mission_id)
    subjects = {TARGET, *("hypothesis:" + ident for ident in (hypotheses or {}))}
    subjects.update("hypothesis:" + item["hypothesis"]["hypothesis_id"]
                    for item in updates if item["expected_revision"] is not None)
    graph_read = {ref: content_hash(await service.get_node(ref)) for ref in subjects}
    task_read, extra = {}, {}
    if source is not None:
        task_read = {source: content_hash(await state.get_task(source))}
        extra = {"source_task_id": source, "source_result_id": source_result}
    tasks = [{
        "task_key": key, "title": "Context probe", "objective": "Use stored context, then observe",
        "role_id": "probe-worker", "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect the response"}],
        "hypothesis_revisions": hypotheses or {},
        "probe_limits": {"max_actions": 1, "max_model_requests": 8}, **extra,
    }] if probe else []
    proposal = HypothesisProbeProposal.model_validate({"updates": list(updates), "tasks": tasks})
    binding = await state.transaction(lambda tx: tx.binding()[1], write=False)
    receipt = await register_probe_tasks(
        session.deps, proposal_id=key, proposal=proposal, expected_revision=mission.revision,
        expected_binding_hash=binding, graph_read=graph_read, known_targets={TARGET},
        known_principals=set(), max_tasks=4, task_read=task_read,
    )
    return receipt["task_ids"][0] if probe else None


async def _world(session):
    """A candidate at revision 1, a source Task with a published result, and the worker."""
    await _register(session, "seed", updates=[_update(None, "Access may be unguarded")],
                    probe=False)
    source = await _register(session, "source")
    state = session.deps.central_state
    claimed = await state.claim_task(source, owner="source-worker", instance_id="source-instance")
    partial = result_for(claimed, outcome="inconclusive", summary="Earlier response observed",
                         evidence_ids=[], observations=["Earlier observation"])
    published = await state.transaction(lambda tx: tx.publish_result(
        source, partial, owner="source-worker", version=claimed.ownership_version,
        expected_hash=content_hash(claimed),
    ))
    worker = await _register(session, "worker", source=source,
                             source_result=published.result.result_id, hypotheses={CANDIDATE: 1})
    return source, worker


async def _revise_candidate(session):
    """A planner update admitted while the worker is mid-turn."""
    await _register(session, "revise", updates=[_update(1, "Revised premise")], probe=False)


def _active_task(messages):
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            context = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            return None
        return (context.get("active_task") or {}).get("task_id")
    return None


def _query(call_id, arguments):
    return NativeCompletion(tool_calls=(NativeToolCall(
        call_id=call_id, name=CONTEXT_TOOL, arguments=arguments),), total_tokens=10)


def _refusal_code(result):
    assert result["ok"] is False, result
    payload = result["payload"]
    return payload.get("code") or payload["diagnostic"]["code"]


class ScriptedContextWorker(FakeLLMClient):
    """Scripted native turns for one worker Task; every other Task gets a text-only turn."""

    def __init__(self):
        super().__init__()
        self.worker, self.script = None, None
        self.advertised, self.results, self.observed = [], {}, {}

    def bind(self, worker, script):
        self.worker, self.script = worker, script

    async def complete_native(self, messages, tools, system=None, **kwargs):
        if _active_task(messages) != self.worker:
            return NativeCompletion(text="No action for this purpose.")
        self.advertised.append({spec.name for spec in tools})
        for message in messages:
            if message.get("role") == "tool":
                self.results[message["tool_call_id"]] = json.loads(message["content"])
        return await self.script(len(self.advertised) - 1, self)


def _happy_script(source, *, session):
    async def script(step, llm):
        if step == 0:
            # Admitted during the worker's turn so it is never a second ready Task.
            llm.observed["sibling"] = await _register(
                session, "sibling", hypotheses={CANDIDATE: 1})
            return _query("list", {"kind": "task", "mode": "list", "count": 8})
        if step == 1:
            records = llm.results["list"]["payload"]["records"]
            llm.observed["list"] = {record["identity"] for record in records}
            ref = next((record["identity"] for record in records
                        if record["identity"] == source), "")
            llm.observed["ref"] = ref
            return _query("read", {"kind": "task", "mode": "read", "section": "completion_criteria",
                                   "refs": [{"kind": "task", "identity": ref}]})
        if step == 2:
            return _query("follow", {"kind": "task", "mode": "related", "hops": 1, "count": 4,
                                     "refs": [{"kind": "task", "identity": llm.observed["ref"]}]})
        return NativeCompletion(text="Source ancestry was reviewed.")
    return script


def _guard_script(case, *, session):
    async def script(step, llm):
        if step == 0:
            if case == "stale":
                await _revise_candidate(session)
            else:
                # Admitted during the worker's turn so it is never a second ready Task.
                # After a revision bump, registering it against rev 1 would be rejected.
                llm.observed["sibling"] = await _register(
                    session, "sibling", hypotheses={CANDIDATE: 1})
            return _query("list", {"kind": "task", "mode": "list", "count": 8})
        if step == 1:
            target = llm.observed["sibling"] if case == "sibling" else llm.worker
            return _query("read", {"kind": "task", "mode": "read", "refs": [
                {"kind": "task", "identity": target}]})
        return NativeCompletion(text="Refusal was recorded.")
    return script


async def test_native_worker_chooses_advertised_context_query_and_reuses_ancestry_ref(tmp_path):
    llm = ScriptedContextWorker()
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        source, worker = await _world(session)
        llm.bind(worker, _happy_script(source, session=session))
        await drive(session)

        assert CONTEXT_TOOL in llm.advertised[0]  # Initial exposure on the first worker turn.
        assert len(llm.advertised) == 4  # Results from each call reached the next model turn.
        sibling = llm.observed["sibling"]
        assert {worker, source} <= llm.observed["list"] and sibling not in llm.observed["list"]
        assert llm.observed["ref"] == source
        read = llm.results["read"]
        assert read["ok"] is True and read["payload"]["view_kind"] == "detail"
        assert "Inspect the response" in json.dumps(read["payload"])  # The stored criterion.
        followup = llm.results["follow"]
        assert followup["ok"] is True
        assert source in {record["identity"] for record in followup["payload"]["records"]}
        assert not tools.inner.invocations  # No target, MCP or network call.


@pytest.mark.parametrize("case", ["sibling", "stale"])
async def test_native_worker_context_query_refuses_sibling_and_stale_candidate_reads(
    tmp_path, case,
):
    llm = ScriptedContextWorker()
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        _, worker = await _world(session)
        llm.bind(worker, _guard_script(case, session=session))
        await drive(session)

        assert CONTEXT_TOOL in llm.advertised[0]
        if case == "sibling":
            sibling = llm.observed["sibling"]
            assert llm.results["list"]["ok"] is True
            assert sibling not in {r["identity"] for r in llm.results["list"]["payload"]["records"]}
            assert _refusal_code(llm.results["read"]) == "context_reference_outside_read_scope"
        else:
            code = _refusal_code(llm.results["list"])
            assert code == "context_worker_candidate_revision_unavailable"
            assert _refusal_code(llm.results["read"]) == code
        assert not tools.inner.invocations
