"""Native graph.record exposes a split review: claim success, candidate hypothesis unknown.

Real native probe loop, real graph.record handler, real structured verifier, and real Planner
admission. Only model turns and the meaning review are scripted. The Planner receives the
receipt, keeps the candidate open, and cannot confirm it from that receipt.
"""

import pytest

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus
from redteam.core.ids import EvidenceId
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_hypothesis_planner import candidate, held, selection
from tests.integration.test_native_candidate_review_loop import (
    NativeReviewLLM,
    _admit,
    _script,
    _task,
)
from tests.integration.test_probe_workers import drive


class SplitReviewLLM(NativeReviewLLM):
    """The claim is supported, but the reference candidate remains unresolved."""

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "meaning" in response_schema["properties"]:
            self.meaning_calls += 1
            return {
                "meaning": "The preserved response supports the claim, not the candidate",
                "outcome": "success",
                "reason": "Scripted split independent meaning review",
                "claim_supported": True,
                "confidence": 0.9,
                "hypothesis_outcome": "unknown",
            }
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


async def test_native_record_delivers_claim_success_with_unknown_candidate_outcome(tmp_path):
    opening = {"updates": [candidate()], "tasks": [_task()],
               "reason": "Observe the candidate's smallest uncertainty"}
    llm = SplitReviewLLM(outcome="success", decisions=[opening])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
        current, = await service.list_hypotheses()
        llm.worker, llm.script = task_id, _script(session, tools, current.hypothesis_id)

        result, = await drive(session)
        assert result.kind == "completed", result.error

        record = llm.results["record"]
        assert record["ok"] is True, record
        finding, = record["payload"]["findings"]
        review = finding["review"]
        assert review["outcome"] == "success"
        assert review["hypothesis_outcome"] == "unknown"

        receipt_id = finding["receipt_id"]
        receipt = await session.deps.evidence_store.get(EvidenceId(receipt_id))
        assert receipt.metadata["candidate_revision"] == current.revision
        assert receipt.metadata["candidate_purpose_hash"] == content_hash(
            hypothesis_purpose(current))
        assert receipt.metadata["hypothesis_outcome"] == "unknown"
        assert llm.meaning_calls == 1

        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "open" and saved.revision == current.revision

        source = llm.observed["source"]
        llm.decisions.append(held("Review did not settle the candidate"))
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        matches = [item for item in llm.planner_inputs[-1]["review_receipts"]
                   if item["evidence_id"] == receipt_id]
        assert len(matches) == 1 and matches[0]["metadata"]["hypothesis_outcome"] == "unknown"
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "open" and saved.revision == current.revision
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE

        with pytest.raises(StateConflict, match="candidate_verdict_unproven"):
            await _admit(session, "split-confirm", candidate(
                current=saved.model_dump(mode="json"), status="confirmed",
                evidence=[str(source.evidence_id)], review=receipt_id,
            ))
