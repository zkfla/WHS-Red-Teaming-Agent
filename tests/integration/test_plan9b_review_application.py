"""Planner consumes canonical review application; Atomizer failure cannot undo it."""

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import AgentPhase
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_candidate_review_selection_repair import (
    RepairLLM,
    _judgment,
    _opening,
)
from tests.integration.test_candidate_subject_repair import _atomize
from tests.integration.test_central_development import TARGET
from tests.integration.test_hypothesis_planner import candidate
from tests.integration.test_observation_candidate_chain_06d import (
    MeaningLLM,
    _graph_record,
    _meaning,
    _observe_candidate,
    _receipt,
)
from tests.integration.test_probe_workers import selection


def _follow_up(candidate_id: str) -> dict:
    return {
        "updates": [],
        "questions": [{
            "question_id": "q-session-impact",
            "objective": "Determine whether the confirmed bypass issues a usable session",
            "target_node_ids": [TARGET],
            "candidate_refs": [candidate_id],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Observe session issuance for the confirmed bounded claim",
            }],
        }],
        "reason": "The confirmed Finding does not establish session impact",
        "termination": "continue",
    }


def _provider_500(_payload):
    raise RuntimeError("synthetic provider 500")


async def test_atomizer_provider_failure_preserves_applied_candidate_finding_and_outbox(tmp_path):
    llm = RepairLLM([_opening("split")], atoms=[_atomize])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        current, source = await _observe_candidate(session, llm)
        verifier = StructuredLLMAnalysisVerifier(
            MeaningLLM([_meaning("success")]), session.deps.evidence_store, service,
        )
        receipt = await _receipt(session, await _graph_record(
            session, verifier, candidate_id=current.hypothesis_id, source=source,
        ))

        applied = await service.get_hypothesis(current.hypothesis_id)
        finding, = await service.list_findings()
        applications = await session.deps.central_state.candidate_review_applications()
        promotions = await session.deps.central_state.candidate_finding_promotions()
        outbox = await session.deps.central_state.transaction(
            lambda tx: list(tx.db.execute(
                "SELECT event_id,sequence,payload FROM state_outbox "
                "WHERE run_id=? ORDER BY sequence",
                (tx.run_id,),
            )),
            write=False,
        )
        assert applied.status == "confirmed" and applied.revision == 2
        assert applications[0]["review_receipt_id"] == str(receipt.evidence_id)
        assert applications[0]["status"] == "promoted"

        llm.plans.append(_follow_up(current.hypothesis_id))
        llm.atoms.extend(
            [_provider_500] * (session.deps.settings.decision_max_attempts + 2)
        )
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

        assert failed.value.stage == "task_atomization"
        projected, = llm.plan_inputs[-1]["candidate_review_state"]["records"]
        assert projected["application_status"] == "promoted"
        assert projected["canonical_candidate_status"] == "confirmed"
        assert projected["finding_id"] == finding.finding_id
        assert await service.get_hypothesis(current.hypothesis_id) == applied
        assert await service.list_findings() == [finding]
        assert await session.deps.central_state.candidate_review_applications() == applications
        assert await session.deps.central_state.candidate_finding_promotions() == promotions
        assert await session.deps.central_state.transaction(
            lambda tx: list(tx.db.execute(
                "SELECT event_id,sequence,payload FROM state_outbox "
                "WHERE run_id=? ORDER BY sequence",
                (tx.run_id,),
            )),
            write=False,
        ) == outbox
        assert not any(
            task.source_question_id == "q-session-impact"
            for task in await session.deps.central_state.list_tasks()
        )

        failed_runtime = session.runtime.model_copy(update={
            "phase": AgentPhase.FAILED,
            "termination_reason": "planning_error",
            "planning_diagnostic": failed.value.diagnostic,
        })
        report = await ReportingAgent().project(
            failed_runtime,
            graph=service,
            evidence_store=session.deps.evidence_store,
            work_store=None,
            task_store=session.deps.central_state,
            central_state=session.deps.central_state,
        )
        assert report.termination_reason == "planning_error"
        assert [item.finding_id for item in report.findings] == [finding.finding_id]
        assert report.candidate_review_applications[0].status == "promoted"

        # A later Planner validation exhaustion still cannot reopen or re-revise the result.
        forbidden = candidate(
            current=applied.model_dump(mode="json"),
            status="confirmed",
            evidence=[str(source.evidence_id)],
            review=str(receipt.evidence_id),
        )
        llm.plans.extend(
            _judgment(
                "split", forbidden,
                reason="Attempt to repeat a terminal verdict already owned by central apply",
            )
            for _ in range(session.deps.settings.decision_max_attempts + 2)
        )
        with pytest.raises(DecisionFailure) as planner_failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)
        assert planner_failed.value.stage == "hypothesis_planning"
        assert planner_failed.value.diagnostic.last_cause.code == (
            "planner_review_application_owned_by_code"
        )
        assert await service.get_hypothesis(current.hypothesis_id) == applied
        assert await service.list_findings() == [finding]
        assert await session.deps.central_state.candidate_review_applications() == applications
        assert await session.deps.central_state.candidate_finding_promotions() == promotions
