"""Native worker reaches a public graph.record review receipt, then the Planner consumes it.

Real native probe loop, real tool.get_detail discovery, real GraphRecordingHandler, real
StructuredLLMAnalysisVerifier, and real Planner admission. Only model turns are scripted, and
the review meaning comes from an explicit fake meaning model. This is protocol evidence, not a
provider proof. The only target call is the one synthetic original inspection.
"""

import json

import pytest

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus
from redteam.core.ids import EvidenceId
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.graph_recording import GRAPH_RECORD_TOOL_ID
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from redteam.orchestration.tool_discovery import TOOL_GET_DETAIL_ID
from tests.integration.test_central_development import TARGET, TOOL, call, inspect, turn
from tests.integration.test_hypothesis_planner import candidate, held, probe, selection
from tests.integration.test_probe_workers import drive

DETAIL = exposed_tool_name(TOOL_GET_DETAIL_ID)
RECORD = exposed_tool_name(GRAPH_RECORD_TOOL_ID)
STATEMENT = "The preserved response reflects the candidate premise"


def _task(key="observe"):
    # One inspection exhausts the physical cap; remaining budget still permits local review.
    return {**probe(key, candidate="local"),
            "probe_limits": {"max_actions": 1, "max_model_requests": 8}}


def _active_task(messages):
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            context = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(context, dict):
            continue
        task = context.get("active_task")
        if isinstance(task, dict) and task.get("task_id"):
            return task["task_id"]
    return None


def _meaning(outcome):
    return {
        "meaning": "The preserved response " + outcome + " the candidate premise",
        "outcome": outcome,
        "reason": "Scripted independent meaning review",
        "claim_supported": outcome == "success",
        "confidence": 0.9,
        "hypothesis_outcome": outcome,
    }


class NativeReviewLLM(FakeLLMClient):
    """Scripted worker turns and Planner decisions, plus an explicit fake meaning model."""

    def __init__(self, *, outcome, decisions):
        super().__init__()
        self.outcome = outcome
        self.decisions = list(decisions)
        self.worker = None
        self.script = None
        self.advertised = []
        self.results = {}
        self.observed = {}
        self.planner_inputs = []
        self.meaning_calls = 0

    async def complete_native(self, messages, tools, system=None, **kwargs):
        if _active_task(messages) != self.worker:
            return NativeCompletion(text="No action for this purpose.")
        self.advertised.append({spec.name for spec in tools})
        for message in messages:
            if message.get("role") == "tool":
                self.results[message["tool_call_id"]] = json.loads(message["content"])
        return await self.script(len(self.advertised) - 1, self)

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema["properties"]
        if "meaning" in properties:
            self.meaning_calls += 1
            return _meaning(self.outcome)
        if "updates" in properties:
            body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
            self.planner_inputs.append(json.loads(body))
            return self.decisions.pop(0)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


def _script(session, tools, hypothesis_id):
    """Model turns in the order a native worker would take them."""

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
            records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
            found = [item for item in records
                     if item.metadata.get("mission_task_id") == llm.worker
                     and isinstance(item.metadata.get("tool_result"), dict)]
            llm.observed["source"] = found[0] if len(found) == 1 else None
            refs = [str(found[0].evidence_id)] if len(found) == 1 else []
            return turn(call("record", GRAPH_RECORD_TOOL_ID, {"findings": [{
                "statement": STATEMENT, "evidence_refs": refs,
                "target_node_id": TARGET, "hypothesis_id": hypothesis_id,
            }]}))
        llm.observed["invocations_after_record"] = len(tools.inner.invocations)
        return NativeCompletion(text="Observation recorded for the candidate.")

    return script


async def _admit(session, identity, update):
    """Direct canonical candidate admission, the same boundary the Planner writes reach."""
    deps, state = session.deps, session.deps.central_state
    service = AttackGraphService(deps.graph_store)
    mission = await state.get_mission(session.runtime.mission_id)
    binding = await state.transaction(lambda tx: tx.binding()[1], write=False)
    ref = "hypothesis:" + update["hypothesis"]["hypothesis_id"]
    graph_read = {TARGET: content_hash(await service.get_node(TARGET)),
                  ref: content_hash(await service.get_node(ref))}
    return await register_probe_tasks(
        deps, proposal_id=identity,
        proposal=HypothesisProbeProposal.model_validate({"updates": [update]}),
        expected_revision=mission.revision, expected_binding_hash=binding,
        graph_read=graph_read, known_targets={TARGET}, known_principals=set(), max_tasks=1,
    )


@pytest.mark.parametrize("outcome", ["success", "unknown"])
async def test_native_worker_discovers_graph_record_and_planner_consumes_its_receipt(
    tmp_path, outcome,
):
    opening = {"updates": [candidate()], "tasks": [_task()],
               "reason": "Observe the candidate's smallest uncertainty"}
    llm = NativeReviewLLM(outcome=outcome, decisions=[opening])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
        current, = await service.list_hypotheses()
        assert current.revision == 1 and current.status == "open"
        llm.worker, llm.script = task_id, _script(session, tools, current.hypothesis_id)

        result, = await drive(session)
        assert result.kind == "completed", result.error

        # graph.record is hidden until the worker fetches its detail through tool.get_detail.
        assert DETAIL in llm.advertised[0]
        assert len(llm.advertised) == 5
        assert not any(RECORD in names for names in llm.advertised[:3])
        assert RECORD in llm.advertised[3]
        detail = llm.results["detail-record"]
        assert detail["ok"] is True
        assert "findings" in detail["payload"]["input_schema"]["properties"]

        # The record turn cites the retained original and the admitted candidate.
        source = llm.observed["source"]
        assert source is not None
        record = llm.results["record"]
        assert record["ok"] is True, record
        finding, = record["payload"]["findings"]
        receipt_id = finding["receipt_id"]
        receipt = await session.deps.evidence_store.get(EvidenceId(receipt_id))
        assert receipt.metadata["candidate_revision"] == 1
        assert receipt.metadata["candidate_purpose_hash"] == content_hash(
            hypothesis_purpose(current))
        assert receipt.metadata["hypothesis_outcome"] == outcome
        assert llm.meaning_calls == 1
        # Posthoc review adds no target execution after the original inspection.
        assert llm.observed["invocations_after_record"] == llm.observed["invocations_before_record"]
        # A Task observation and its receipt never confirm or refute the candidate by themselves.
        after_worker = await service.get_hypothesis(current.hypothesis_id)
        assert after_worker.status == "open" and after_worker.revision == 1

        def matching_receipts(wake):
            return [item for item in wake["review_receipts"] if item["evidence_id"] == receipt_id]

        if outcome == "unknown":
            llm.decisions.append(held("Review did not settle the candidate"))
            assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
            assert len(llm.planner_inputs) == 2
            matches = matching_receipts(llm.planner_inputs[-1])
            assert len(matches) == 1 and matches[0]["metadata"]["hypothesis_outcome"] == "unknown"
            saved = await service.get_hypothesis(current.hypothesis_id)
            assert saved.status == "open" and saved.revision == 1
            mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
            assert mission.status is MissionStatus.ACTIVE
            # UNKNOWN never admits a verdict, even when a confirmation is proposed directly.
            with pytest.raises(StateConflict, match="candidate_verdict_unproven"):
                await _admit(session, "unknown-confirm", candidate(
                    current=saved.model_dump(mode="json"), status="confirmed",
                    evidence=[str(source.evidence_id)], review=receipt_id,
                ))
            assert await service.list_findings() == []
            assert await session.deps.central_state.candidate_finding_promotions() == []
            return

        judged = candidate(current=current.model_dump(mode="json"), status="confirmed",
                           evidence=[str(source.evidence_id)], review=receipt_id)
        llm.decisions.append({"updates": [judged], "termination": "replan",
                              "reason": "The bound independent review supports this verdict"})
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.planner_inputs) == 2
        matches = matching_receipts(llm.planner_inputs[-1])
        assert len(matches) == 1
        assert matches[0]["metadata"]["hypothesis_outcome"] == "success"
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "confirmed" and saved.revision == 2
        findings = await service.list_findings()
        assert len(findings) == 1
        finding, = findings
        assert finding.verified is True
        assert set(finding.evidence_ids) == {str(source.evidence_id), receipt_id}
        assert finding.metadata["hypothesis_id"] == current.hypothesis_id
        assert finding.metadata["review_receipt_id"] == receipt_id
        assert finding.metadata["candidate_pre_verdict_revision"] == 1
        assert finding.metadata["candidate_revision"] == 2
        promotions = await session.deps.central_state.candidate_finding_promotions()
        assert len(promotions) == 1
        assert promotions[0]["status"] == "promoted"
        assert promotions[0]["finding_id"] == finding.finding_id
