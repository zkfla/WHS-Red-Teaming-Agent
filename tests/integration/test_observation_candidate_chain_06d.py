"""06D: observation review receipt and direct central candidate application.

Real Task review, real GraphRecordingHandler, real StructuredLLMAnalysisVerifier, and real
canonical admission. Only model answers are scripted. The verifier meaning answers come from a
labelled fake model, so this file is protocol evidence and not a provider proof.
"""

import json

import pytest

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.central import GraphTransaction
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, EvidenceId, RunId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.analysis import (
    StructuredLLMAnalysisVerifier,
    append_posthoc_claim_receipt,
)
from redteam.orchestration.candidate_review_application import apply_candidate_review_receipt
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.graph_recording import GraphRecordingHandler
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_development import TARGET, TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import candidate, held, probe, selection
from tests.integration.test_probe_workers import ReviewedObservationModel, drive, register


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


def _meaning(outcome, *, confidence=0.9):
    return {
        "meaning": "The preserved response " + outcome + " the candidate premise",
        "outcome": outcome,
        "reason": "Scripted independent meaning review",
        "claim_supported": outcome == "success",
        "confidence": confidence,
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
async def test_observation_review_receipt_directly_applies_only_a_terminal_outcome(
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
        assert receipt.metadata["candidate_revision"] == 1
        assert receipt.metadata["candidate_purpose_hash"] == content_hash(
            hypothesis_purpose(current))
        assert receipt.metadata["hypothesis_outcome"] == outcome
        application = result.payload["findings"][0]["application"]
        saved = await service.get_hypothesis(current.hypothesis_id)

        if outcome == "unknown":
            assert application["status"] == "inconclusive"
            assert saved.status == "open" and saved.revision == 1
            # The producer's review-outcome gate rejects UNKNOWN before canonical receipt binding.
            with pytest.raises(StateConflict, match="candidate_verdict_unproven"):
                await _commit(session, "unknown-confirm", candidate(
                    current=saved.model_dump(mode="json"), status="confirmed",
                    evidence=[str(source.evidence_id)], review=str(receipt.evidence_id),
                ))
            assert await service.list_findings() == []
            return

        status = "confirmed" if outcome == "success" else "rejected"
        assert saved.revision == 2 and saved.status == status
        findings = await service.list_findings()
        promotions = await session.deps.central_state.candidate_finding_promotions()
        if outcome == "success":
            assert application["status"] == "promoted"
            assert len(findings) == 1
            finding = findings[0]
            assert finding.verified is True
            assert set(finding.evidence_ids) == {
                str(source.evidence_id), str(receipt.evidence_id),
            }
            assert finding.metadata["hypothesis_id"] == current.hypothesis_id
            assert finding.metadata["review_receipt_id"] == str(receipt.evidence_id)
            assert finding.metadata["candidate_pre_verdict_revision"] == 1
            assert finding.metadata["candidate_revision"] == 2
            assert finding.metadata["target"] == TARGET
            assert len(promotions) == 1
            assert promotions[0]["status"] == "promoted"
            assert promotions[0]["finding_id"] == finding.finding_id
        else:
            assert application["status"] == "candidate_rejected"
            assert findings == []
            assert len(promotions) == 1
            assert promotions[0]["status"] == "candidate_rejected"
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
        replay = await apply_candidate_review_receipt(
            session.deps, str(receipt.evidence_id),
        )
        assert replay.status == "already_applied"
        assert replay.candidate_revision == 2
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 2


@pytest.mark.parametrize(
    ("confidence", "expected_status", "expected_findings"),
    [(0.8, "promoted", 1), (0.79, "inconclusive", 0)],
)
async def test_finding_promotion_uses_eighty_percent_review_threshold(
    tmp_path, confidence, expected_status, expected_findings,
):
    opening = {"updates": [candidate()], "tasks": [probe(candidate="local")],
               "reason": "Observe the candidate's smallest uncertainty"}
    async with selection(tmp_path, llm=ChainLLM([opening])) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        current, source = await _observe_candidate(session, llm)
        verifier = StructuredLLMAnalysisVerifier(
            MeaningLLM([_meaning("success", confidence=confidence)]),
            session.deps.evidence_store,
            service,
        )
        result = await _graph_record(
            session, verifier, candidate_id=current.hypothesis_id, source=source,
        )
        receipt = await _receipt(session, result)

        assert receipt.metadata["review_contract_version"] == 4
        assert receipt.metadata["confidence"] == confidence
        application = result.payload["findings"][0]["application"]
        assert application["status"] == expected_status
        assert application["review_confidence"] == confidence
        assert len(await service.list_findings()) == expected_findings
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == ("confirmed" if confidence >= 0.8 else "open")
        if expected_findings:
            finding, = await service.list_findings()
            assert finding.metadata["review_confidence"] == confidence
            assert finding.metadata["minimum_finding_confidence"] == 0.8


async def _receipts(session):
    records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
    return [item for item in records if item.metadata.get("record_kind") == "review_receipt"]


async def test_exact_confirmation_replay_keeps_one_v2_finding(tmp_path):
    opening = {"updates": [candidate()], "tasks": [probe(candidate="local")],
               "reason": "Observe the candidate's smallest uncertainty"}
    async with selection(tmp_path, llm=ChainLLM([opening])) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        current, source = await _observe_candidate(session, llm)
        verifier = StructuredLLMAnalysisVerifier(
            MeaningLLM([_meaning("success")]), session.deps.evidence_store, service,
        )
        receipt = await _receipt(session, await _graph_record(
            session, verifier, candidate_id=current.hypothesis_id, source=source,
        ))
        first = await apply_candidate_review_receipt(
            session.deps, str(receipt.evidence_id),
        )
        replay = await apply_candidate_review_receipt(
            session.deps, str(receipt.evidence_id),
        )

        assert first.status == replay.status == "already_applied"
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 2
        assert len(await service.list_findings()) == 1
        assert len(await session.deps.central_state.candidate_finding_promotions()) == 1


@pytest.mark.parametrize("after_finding_write", [False, True])
async def test_v2_finding_failure_rolls_back_candidate_and_retry_converges(
    tmp_path, monkeypatch, after_finding_write,
):
    opening = {"updates": [candidate()], "tasks": [probe(candidate="local")],
               "reason": "Observe the candidate's smallest uncertainty"}
    async with selection(tmp_path, llm=ChainLLM([opening])) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        current, source = await _observe_candidate(session, llm)
        verifier = StructuredLLMAnalysisVerifier(
            MeaningLLM([_meaning("success")]), session.deps.evidence_store, service,
        )
        original = GraphTransaction.save_finding

        def fail(self, *args, **kwargs):
            if after_finding_write:
                original(self, *args, **kwargs)
            raise RuntimeError("simulated candidate/finding transaction crash")

        monkeypatch.setattr(GraphTransaction, "save_finding", fail)
        reviewed = await _graph_record(
            session, verifier, candidate_id=current.hypothesis_id, source=source,
        )
        receipt = await _receipt(session, reviewed)
        application = reviewed.payload["findings"][0]["application"]
        assert application["status"] == "retryable_error"
        assert application["reason"] == "RuntimeError"
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1
        assert await service.list_findings() == []
        assert await session.deps.central_state.candidate_finding_promotions() == []

        monkeypatch.setattr(GraphTransaction, "save_finding", original)
        retried = await apply_candidate_review_receipt(
            session.deps, str(receipt.evidence_id),
        )
        assert retried.status == "promoted"
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 2
        assert len(await service.list_findings()) == 1


@pytest.mark.parametrize("shape", ["new_proposal", "other_candidate"])
async def test_out_of_purpose_probe_is_refused_before_review_or_writes(tmp_path, shape):
    async with selection(tmp_path, llm=ChainLLM([])) as (session, llm, tools):
        state, service = session.deps.central_state, AttackGraphService(session.deps.graph_store)
        await _commit(session, "local-open", candidate("local"))
        await _commit(session, "other-open", candidate("other"))
        # The probe is admitted for the local candidate only; "other" is outside its purpose.
        task_id = await register(session, "scoped", hypotheses={"local": 1})
        worker, = await ready_workers(session.deps, session.runtime)
        # Claiming the worker is orchestration's own step; the refusal must leave that state alone.
        before = (await state.get_task(task_id)).model_dump(mode="json")
        meaning = MeaningLLM([])
        verifier = StructuredLLMAnalysisVerifier(meaning, session.deps.evidence_store, service)
        handler = GraphRecordingHandler(
            evidence_store=session.deps.evidence_store, graph_service=service, verifier=verifier,
        )
        if shape == "new_proposal":
            tool_input = {"hypotheses": [{"statement": "Start a different investigation"}]}
        else:
            tool_input = {"findings": [{
                "statement": "The other candidate is reflected in the response",
                "evidence_refs": [], "target_node_id": TARGET, "hypothesis_id": "other",
            }]}
        result = await handler(tool_input, LocalToolContext(session.deps, worker))
        assert not result.ok
        expected = ("probe_candidate_change_requires_planner" if shape == "new_proposal"
                    else "probe_finding_candidate_reference")
        assert result.payload["diagnostic"]["code"] == expected
        assert meaning.review_calls == 0
        assert await _receipts(session) == []
        assert await service.list_findings() == []
        assert (await service.get_hypothesis("local")).revision == 1
        assert (await service.get_hypothesis("other")).revision == 1
        assert (await state.get_task(task_id)).model_dump(mode="json") == before


@pytest.mark.parametrize("breach", ["run", "engagement", "unlinkable"])
async def test_foreign_or_unlinkable_origin_never_produces_a_receipt(tmp_path, breach):
    llm = ChainLLM([{"updates": [candidate()], "tasks": [probe(candidate="local")],
                     "reason": "Observe the candidate's smallest uncertainty"}])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        state, service = session.deps.central_state, AttackGraphService(session.deps.graph_store)
        current, source = await _observe_candidate(session, llm)
        task_id = source.metadata["mission_task_id"]
        task_before = (await state.get_task(task_id)).model_dump(mode="json")
        # A real stored copy of the preserved source, changed only in the origin under test.
        changes = {
            "run": {"run_id": RunId("06d-foreign-run")},
            "engagement": {"engagement_id": EngagementId("06d-foreign-engagement")},
            "unlinkable": {"action_id": None, "invocation_id": None},
        }[breach]
        cited = source.model_copy(deep=True, update={
            "evidence_id": EvidenceId("06d-" + breach + "-origin"), **changes,
        })
        await session.deps.evidence_store.append(cited)
        receipts_before = len(await _receipts(session))
        meaning = MeaningLLM([])
        verifier = StructuredLLMAnalysisVerifier(meaning, session.deps.evidence_store, service)
        result = await _graph_record(session, verifier, candidate_id=current.hypothesis_id,
                                     source=cited)
        finding, = result.payload["findings"]
        expected = {"run": "other run", "engagement": "other run",
                    "unlinkable": "origin unlinkable"}[breach]
        assert finding["status"] == "refused" and finding["reason"] == expected
        assert finding.get("receipt_id") is None
        assert meaning.review_calls == 0
        # The real verifier and the real receipt producer must refuse the same cited origin.
        claim = {"statement": "The preserved response reflects the candidate premise",
                 "target_node_id": TARGET, "hypothesis_id": current.hypothesis_id,
                 "principal_refs": []}
        direct_meaning = MeaningLLM([])
        direct = StructuredLLMAnalysisVerifier(
            direct_meaning, session.deps.evidence_store, service,
        )
        assessment = await direct.verify_claim_with_budget(
            claim=claim, evidence_ids=[str(cited.evidence_id)],
            run_id=str(session.runtime.run_id), budget=DecisionBudget(), security_claim=True,
        )
        # The verifier's own origin and grounding checks refuse before any meaning call.
        assert assessment.outcome == "unknown", assessment.reason
        assert direct_meaning.review_calls == 0
        receipt = await append_posthoc_claim_receipt(
            evidence_store=session.deps.evidence_store, run_id=str(session.runtime.run_id),
            engagement_id=str(session.runtime.engagement_id), claim=claim,
            evidence_ids=[str(cited.evidence_id)], assessment=assessment,
            hypothesis_id=current.hypothesis_id, action_id=str(source.action_id),
            invocation_id=str(source.invocation_id), target_node_id=TARGET, candidate=current,
        )
        assert receipt is None
        assert len(await _receipts(session)) == receipts_before
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1
        assert await service.list_findings() == []
        assert (await state.get_task(task_id)).model_dump(mode="json") == task_before
        mission = await state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
