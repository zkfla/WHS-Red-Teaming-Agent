"""Native probe worker corrects a refused graph.record candidate reference, then reaches review.

Real native probe loop, real tool.get_detail discovery, real GraphRecordingHandler, real
StructuredLLMAnalysisVerifier, and real Planner admission. The worker turns and Planner decisions
are scripted, and the review meaning comes from the explicit fake meaning model of the native
review loop. This is protocol evidence, not a provider proof, and no live model is called. The
only target call is the one synthetic original inspection.
"""

import json

import pytest

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.attack_graph.service import AttackGraphService
from redteam.core.ids import EvidenceId
from redteam.core.ports.llm import NativeCompletion
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.graph_recording import GRAPH_RECORD_TOOL_ID
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.tool_discovery import TOOL_GET_DETAIL_ID
from tests.integration.test_central_development import TARGET, TOOL, call, inspect, turn
from tests.integration.test_hypothesis_planner import candidate, selection
from tests.integration.test_native_candidate_review_loop import NativeReviewLLM, _task
from tests.integration.test_probe_workers import drive

RECORD = exposed_tool_name(GRAPH_RECORD_TOOL_ID)
PROBE_FINDING = "probe_finding_candidate_reference"
PROBE_HYPOTHESES = "probe_candidate_change_requires_planner"
STATEMENT = "The preserved response reflects the candidate premise"
GHOST = "ghost-candidate-zz"


def _recording(refs, hypothesis_id):
    finding = {"statement": STATEMENT, "evidence_refs": refs, "target_node_id": TARGET}
    if hypothesis_id is not None:
        finding["hypothesis_id"] = hypothesis_id
    return {"findings": [finding]}


async def _evidence_ids(session):
    records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
    return [str(item.evidence_id) for item in records]


async def _source(session, worker):
    records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
    found = [item for item in records
             if item.metadata.get("mission_task_id") == worker
             and isinstance(item.metadata.get("tool_result"), dict)]
    return found[0] if len(found) == 1 else None


def _script(session, tools, bad_ref, good_ref):
    """Model turns in the order a native worker takes them, with one refused record call."""

    async def script(step, llm):
        if step == 0:
            return turn(call("detail-target", TOOL_GET_DETAIL_ID, {"tool_id": TOOL}))
        if step == 1:
            return turn(inspect("observe"))
        if step == 2:
            llm.observed["invocations_before_record"] = len(tools.inner.invocations)
            return turn(call("detail-record", TOOL_GET_DETAIL_ID,
                             {"tool_id": GRAPH_RECORD_TOOL_ID}))
        if step == 3:
            source = await _source(session, llm.worker)
            llm.observed["source"] = source
            refs = [str(source.evidence_id)] if source is not None else []
            llm.observed["refs"] = refs
            llm.observed["ids_before_bad"] = await _evidence_ids(session)
            return turn(call("record-bad", GRAPH_RECORD_TOOL_ID, _recording(refs, bad_ref)))
        if step == 4:
            # The refused result must be visible to this model turn before any correction.
            llm.observed["feedback"] = llm.results.get("record-bad")
            llm.observed["meaning_before_correction"] = llm.meaning_calls
            llm.observed["ids_after_bad"] = await _evidence_ids(session)
            return turn(call("record-good", GRAPH_RECORD_TOOL_ID,
                             _recording(llm.observed["refs"], good_ref)))
        llm.observed["invocations_after_record"] = len(tools.inner.invocations)
        return NativeCompletion(text="Observation recorded for the candidate.")

    return script


@pytest.mark.parametrize("bad_ref", [None, GHOST], ids=["missing", "unknown"])
async def test_native_worker_corrects_refused_candidate_reference_then_reaches_review(
    tmp_path, bad_ref,
):
    opening = {"updates": [candidate()], "tasks": [_task()],
               "reason": "Observe the candidate's smallest uncertainty"}
    llm = NativeReviewLLM(outcome="success", decisions=[opening])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
        current, = await service.list_hypotheses()
        assert current.revision == 1 and current.status == "open"
        llm.worker = task_id
        llm.script = _script(session, tools, bad_ref, current.hypothesis_id)

        result, = await drive(session)
        assert result.kind == "completed", result.error

        # Turn order: discover the target, inspect once, discover graph.record, refuse, correct.
        assert len(llm.advertised) == 6
        assert not any(RECORD in names for names in llm.advertised[:3])
        assert all(RECORD in names for names in llm.advertised[3:])

        # The refused call names the candidate reference problem and nothing else.
        bad = llm.results["record-bad"]
        assert bad["ok"] is False
        diagnostic = bad["payload"]["diagnostic"]
        location = "$.findings[0].hypothesis_id"
        rule = ("required_admitted_candidate_id" if bad_ref is None
                else "admitted_candidate_id_in_hypothesis_revisions")
        assert diagnostic["code"] == PROBE_FINDING
        assert diagnostic["code"] != PROBE_HYPOTHESES
        assert diagnostic["retryable"] is True
        assert diagnostic["cause_scope"] == "model_input"
        assert diagnostic["owner"] == "graphrecording"
        assert diagnostic["location"] == location
        assert diagnostic["validation_issues"] == [{"rule": rule, "location": location}]
        refused_text = json.dumps(bad)
        assert STATEMENT not in refused_text
        if bad_ref is not None:
            assert bad_ref not in refused_text

        # The next model turn received that same refused result, and the refusal wrote no graph
        # state. Only the refused call's own tool-result envelope may be added to evidence.
        assert llm.observed["feedback"] == bad
        assert llm.observed["meaning_before_correction"] == 0
        before_ids = llm.observed["ids_before_bad"]
        assert llm.observed["ids_after_bad"][:len(before_ids)] == before_ids
        new_ids = llm.observed["ids_after_bad"][len(before_ids):]
        assert len(new_ids) <= 1
        for evidence_id in new_ids:
            added = await session.deps.evidence_store.get(EvidenceId(evidence_id))
            assert added.metadata["model_tool_call_id"] == "record-bad"
            assert added.metadata["tool_id"] == GRAPH_RECORD_TOOL_ID
            assert added.metadata["record_kind"] == "runtime_tool_result"
        assert await service.list_findings() == []

        # The corrected call uses the admitted candidate and reaches the existing review flow.
        good = llm.results["record-good"]
        assert good["ok"] is True, good
        finding, = good["payload"]["findings"]
        receipt_id = finding["receipt_id"]
        receipt = await session.deps.evidence_store.get(EvidenceId(receipt_id))
        assert receipt.metadata["candidate_revision"] == 1
        assert receipt.metadata["candidate_purpose_hash"] == content_hash(
            hypothesis_purpose(current))
        assert receipt.metadata["hypothesis_outcome"] == "success"
        assert llm.meaning_calls == 1
        assert llm.observed["invocations_after_record"] == llm.observed["invocations_before_record"]
        after_worker = await service.get_hypothesis(current.hypothesis_id)
        assert after_worker.status == "open" and after_worker.revision == 1

        # The Planner consumes the public review receipt from the corrected call.
        source = llm.observed["source"]
        judged = candidate(current=current.model_dump(mode="json"), status="confirmed",
                           evidence=[str(source.evidence_id)], review=receipt_id)
        llm.decisions.append({"updates": [judged], "termination": "replan",
                              "reason": "The bound independent review supports this verdict"})
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.planner_inputs) == 2
        matches = [item for item in llm.planner_inputs[-1]["review_receipts"]
                   if item["evidence_id"] == receipt_id]
        assert len(matches) == 1
        assert matches[0]["metadata"]["hypothesis_outcome"] == "success"
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "confirmed" and saved.revision == 2
        assert await service.list_findings() == []





