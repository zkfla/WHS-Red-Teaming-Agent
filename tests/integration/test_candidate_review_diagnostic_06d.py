"""06D review diagnostics through the real planner finalize path.

Only the model boundary is scripted. Evidence, receipts, candidates and outbox are real.
"""

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import Verdict
from redteam.orchestration.analysis import VerificationAssessment, append_posthoc_claim_receipt
from redteam.orchestration.coordination import CandidateAdmissionConflict
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET, TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import (
    PlannerDouble,
    candidate,
    probe,
    register_candidate_update,
    selection,
)
from tests.integration.test_probe_workers import drive

FIRST = {"updates": [candidate()], "tasks": [probe(candidate="local")],
         "reason": "Observe the candidate's smallest uncertainty"}


async def _observe(session, llm):
    first_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
    llm.native_responses_by_task[first_id] = [discover(TOOL), turn(inspect("observed"))]
    await drive(session)
    return first_id


async def _source(session, task_id):
    sources = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
    return next(item for item in sources if item.metadata.get("mission_task_id") == task_id
                and isinstance(item.metadata.get("tool_result"), dict))


async def _review(session, current, source, reason):
    """A final posthoc receipt for the same claim; a later one supersedes an earlier one."""
    receipt = await append_posthoc_claim_receipt(
        evidence_store=session.deps.evidence_store, run_id=session.runtime.run_id,
        engagement_id=session.runtime.engagement_id,
        claim={"statement": current.description}, evidence_ids=[str(source.evidence_id)],
        assessment=VerificationAssessment(
            verdict=Verdict.PASS, reviewer_id="fixture-independent-review", reason=reason,
            outcome="success", hypothesis_outcome="success",
            source_refs=[str(source.evidence_id)], claim_supported=True,
        ),
        hypothesis_id=current.hypothesis_id, action_id=str(source.action_id),
        invocation_id=str(source.invocation_id), target_node_id=TARGET, candidate=current,
    )
    assert receipt is not None
    return str(receipt.evidence_id)


def _verdict(review, evidence):
    """A confirmation with a follow-up probe, the only planner shape that reaches admission."""
    def decide(payload):
        current, = payload["candidates"]
        return {"updates": [candidate(current=current, status="confirmed",
                                      evidence=evidence, review=review)],
                "tasks": [probe("verify", candidate=current["hypothesis_id"],
                                revision=current["revision"] + 1)],
                "reason": "Confirm the candidate from the cited review"}
    return decide


async def test_stale_review_verdict_is_rejected_before_semantic_receipt_selection(tmp_path):
    llm = PlannerDouble([FIRST])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first_id = await _observe(session, llm)
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        source = await _source(session, first_id)
        stale = await _review(session, current, source, "First review")
        await _review(session, current, source, "Second review supersedes the first")
        llm.decisions.append(_verdict(stale, [str(source.evidence_id)]))
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        feedback = llm.requests[-1]["messages"][-1]["content"].partition(
            "Previous attempt failed"
        )[2]
        diagnostic, marker, replay = feedback.partition("\n\nRejected proposal data:\n")
        assert marker
        assert "planner_review_application_owned_by_code" in diagnostic
        assert stale not in diagnostic and stale in replay
        assert (await service.get_hypothesis(current.hypothesis_id)).status == "open"


async def test_unsafe_missing_review_reference_does_not_reach_durable_diagnostic(tmp_path):
    leaked = "ev missing " + "A" * 150 + " password=hunter2"
    llm = PlannerDouble([FIRST])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first_id = await _observe(session, llm)
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        source = await _source(session, first_id)
        llm.decisions.append(_verdict(leaked, [str(source.evidence_id)]))
        # Repair feedback names the code and location only; the unknown reference is not echoed.
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        prompt = llm.requests[-1]["messages"][-1]["content"]
        feedback = prompt.partition("Previous attempt failed")[2]
        diagnostic, marker, replay = feedback.partition("\n\nRejected proposal data:\n")
        assert marker
        assert "planner_review_application_owned_by_code" in diagnostic
        assert "$.updates[0].review_evidence_id" in diagnostic
        assert "hunter2" not in feedback
        assert "ev missing" not in diagnostic and "ev missing" in replay
        assert (await service.get_hypothesis(current.hypothesis_id)).status == "open"
        assert await service.list_findings() == []
        # Final admission keeps the unknown reference out of its durable diagnostic.
        update = candidate(current=current.model_dump(mode="json"), status="confirmed",
                           evidence=[str(source.evidence_id)], review=leaked)
        with pytest.raises(CandidateAdmissionConflict) as gate:
            await register_candidate_update(session, update)
        diagnostic = gate.value.diagnostic
        assert diagnostic.code == "original_evidence_missing"
        assert diagnostic.evidence_ids == []
        assert "hunter2" not in diagnostic.model_dump_json()
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1


async def test_well_formed_missing_review_reference_is_kept_as_identifier(tmp_path):
    llm = PlannerDouble([FIRST])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first_id = await _observe(session, llm)
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        source = await _source(session, first_id)
        llm.decisions.append(_verdict("ev_missing_review", [str(source.evidence_id)]))
        # The well-formed but unknown identifier is rejected in repair and never echoed back.
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        feedback = llm.requests[-1]["messages"][-1]["content"]
        diagnostic, marker, replay = feedback.partition("\n\nRejected proposal data:\n")
        assert marker and "planner_review_application_owned_by_code" in diagnostic
        assert "ev_missing_review" not in diagnostic and "ev_missing_review" in replay
        assert (await service.get_hypothesis(current.hypothesis_id)).status == "open"
        # The final admission keeps the identifier in its diagnostic for the canonical record.
        update = candidate(current=current.model_dump(mode="json"), status="confirmed",
                           evidence=[str(source.evidence_id)], review="ev_missing_review")
        with pytest.raises(CandidateAdmissionConflict) as gate:
            await register_candidate_update(session, update)
        assert gate.value.diagnostic.evidence_ids == ["ev_missing_review"]
