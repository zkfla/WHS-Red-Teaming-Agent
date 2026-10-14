"""06D: observation Task review, graph.record receipt, and Planner judgment on one candidate.

Real Task review, real GraphRecordingHandler, real StructuredLLMAnalysisVerifier, and real
canonical admission. Only model answers are scripted. The verifier meaning answers come from a
labelled fake model, so this file is protocol evidence and not a provider proof.
"""

import json

import pytest

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EvidenceId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.graph_recording import GraphRecordingHandler
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_development import TARGET, TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import candidate, held, probe, selection
from tests.integration.test_probe_workers import ReviewedObservationModel, drive


class ChainLLM(ReviewedObservationModel):
    """Scripted Planner judgments. The observation Task review keeps the production checker."""

    def __init__(self, decisions):
        super().__init__()
        self.decisions = list(decisions)
        self.planner_inputs = []

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "updates" not in response_schema["properties"]:
            return await super().complete_structured(
                messages=messages, response_schema=response_schema, system=system, **kwargs,
            )
        body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
        payload = json.loads(body)
        self.planner_inputs.append(payload)
        response = self.decisions.pop(0) if self.decisions else held()
        return response(payload) if callable(response) else response


class MeaningLLM(FakeLLMClient):
    """Fake meaning model for the real verifier. Each entry answers one review call."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)
        self.review_calls = 0

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        assert "meaning" in response_schema["properties"]
        self.review_calls += 1
        return dict(self.answers.pop(0))


def _meaning(outcome):
    return {
        "meaning": "The preserved response " + outcome + " the candidate premise",
        "outcome": outcome,
        "reason": "Scripted independent meaning review",
        "claim_supported": outcome == "success",
        "confidence": 0.9,
        "hypothesis_outcome": outcome,
    }


async def _observe_candidate(session, llm):
    """Planner opens one candidate and one observation; the real Task review completes it."""
    service = AttackGraphService(session.deps.graph_store)
    task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
    current, = await service.list_hypotheses()
    assert current.revision == 1 and current.status == "open"
    llm.native_responses_by_task[task_id] = [discover(TOOL), turn(inspect("observe"))]
    result, = await drive(session)
    assert result.kind == "completed", result.error
    task = await session.deps.central_state.get_task(task_id)
    assert task.status is TaskStatus.COMPLETED
    # Task completion never confirms or refutes the candidate.
    after_task = await service.get_hypothesis(current.hypothesis_id)
    assert after_task.status == "open" and after_task.revision == 1
    records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
    source = next(item for item in records if item.metadata.get("mission_task_id") == task_id
                  and isinstance(item.metadata.get("tool_result"), dict))
    return after_task, source


async def _graph_record(session, verifier, *, candidate_id, source):
    """Call the real graph.record handler with the real verifier and stores."""
    context = LocalToolContext(session.deps, session.runtime, budget=DecisionBudget())
    handler = GraphRecordingHandler(
        evidence_store=session.deps.evidence_store,
        graph_service=AttackGraphService(session.deps.graph_store),
        verifier=verifier,
    )
    return await handler({"findings": [{
        "statement": "The preserved response reflects the candidate premise",
        "evidence_refs": [str(source.evidence_id)],
        "target_node_id": TARGET,
        "hypothesis_id": candidate_id,
    }]}, context)


async def _receipt(session, result):
    finding, = result.payload["findings"]
    assert finding["receipt_id"], finding
    return await session.deps.evidence_store.get(EvidenceId(finding["receipt_id"]))


async def _commit(session, identity, update):
    """Direct canonical candidate admission, the same boundary the Planner writes reach."""
    deps, state = session.deps, session.deps.central_state
    service = AttackGraphService(deps.graph_store)
    mission = await state.get_mission(session.runtime.mission_id)
    binding = await state.transaction(lambda tx: tx.binding()[1], write=False)
    graph_read = {TARGET: content_hash(await service.get_node(TARGET))}
    ref = "hypothesis:" + update["hypothesis"]["hypothesis_id"]
    if update.get("expected_revision") is not None:
        graph_read[ref] = content_hash(await service.get_node(ref))
    return await register_probe_tasks(
        deps, proposal_id=identity,
        proposal=HypothesisProbeProposal.model_validate({"updates": [update]}),
        expected_revision=mission.revision, expected_binding_hash=binding,
        graph_read=graph_read, known_targets={TARGET}, known_principals=set(), max_tasks=1,
    )


@pytest.mark.parametrize("outcome", ["success", "refuted", "unknown"])
async def test_observation_review_receipt_then_planner_judgment_admits_only_exact_purpose(
    tmp_path, outcome,
):
    opening = {"updates": [candidate()], "tasks": [probe(candidate="local")],
               "reason": "Observe the candidate's smallest uncertainty"}
    llm = ChainLLM([opening])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        meaning = MeaningLLM([_meaning(outcome)])
        verifier = StructuredLLMAnalysisVerifier(meaning, session.deps.evidence_store, service)
        current, source = await _observe_candidate(session, llm)
        result = await _graph_record(session, verifier, candidate_id=current.hypothesis_id,
                                     source=source)
        receipt = await _receipt(session, result)
        assert meaning.review_calls == 1
        # graph.record only produces the receipt; the candidate itself is untouched.
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1
        assert receipt.metadata["candidate_revision"] == 1
        assert receipt.metadata["candidate_purpose_hash"] == content_hash(
            hypothesis_purpose(current))
        assert receipt.metadata["hypothesis_outcome"] == outcome

        if outcome == "unknown":
            # UNKNOWN never admits a verdict: the Planner holds and a confirmation is rejected.
            llm.decisions.append(held("Review did not settle the candidate"))
            assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
            saved = await service.get_hypothesis(current.hypothesis_id)
            assert saved.status == "open" and saved.revision == 1
            assert len(llm.planner_inputs) == 2
            unknown_receipts = [item for item in llm.planner_inputs[-1]["review_receipts"]
                                if item["evidence_id"] == str(receipt.evidence_id)]
            assert len(unknown_receipts) == 1
            assert unknown_receipts[0]["metadata"]["hypothesis_outcome"] == "unknown"
            mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
            assert mission.status is MissionStatus.ACTIVE
            # The producer's review-outcome gate rejects UNKNOWN before canonical receipt binding.
            with pytest.raises(StateConflict, match="candidate_verdict_unproven"):
                await _commit(session, "unknown-confirm", candidate(
                    current=saved.model_dump(mode="json"), status="confirmed",
                    evidence=[str(source.evidence_id)], review=str(receipt.evidence_id),
                ))
            return

        status = "confirmed" if outcome == "success" else "rejected"
        judged = candidate(current=current.model_dump(mode="json"), status=status,
                           evidence=[str(source.evidence_id)], review=str(receipt.evidence_id))
        wrong = {**judged, "hypothesis": {**judged["hypothesis"],
                                          "premise": "A different untested prerequisite"}}
        with pytest.raises(StateConflict, match="candidate_verdict_receipt_unbound"):
            await _commit(session, "wrong-purpose", wrong)
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1
        llm.decisions.append({"updates": [judged], "termination": "replan",
                              "reason": "The bound independent review supports this verdict"})
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        # The judgment is the second Planner call; the first was the opening observation.
        assert len(llm.planner_inputs) == 2
        wake = llm.planner_inputs[-1]
        candidate_receipts = [item for item in wake["review_receipts"]
                              if item["evidence_id"] == str(receipt.evidence_id)]
        assert len(candidate_receipts) == 1
        meta = candidate_receipts[0]["metadata"]
        assert meta["candidate_revision"] == 1
        assert meta["candidate_purpose_hash"] == content_hash(hypothesis_purpose(current))
        assert meta["hypothesis_outcome"] == outcome
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.revision == 2 and saved.status == status
        assert await service.list_findings() == []
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
        # A receipt bound to revision 1 cannot admit a judgment against revision 2.
        reopened = {**saved.model_dump(mode="json"), "status": "open"}
        with pytest.raises(StateConflict, match="candidate_verdict_receipt_unbound"):
            await _commit(session, "stale-revision", candidate(
                current=reopened, status=status,
                evidence=[str(source.evidence_id)], review=str(receipt.evidence_id),
            ))
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 2
