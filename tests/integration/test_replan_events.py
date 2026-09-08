import pytest

from redteam_core.knowledge.state import KnowledgeSlot, KnowledgeSlotStatus
from redteam_core.models import CandidateDisposition, DecisionAction
from redteam_core.models.decision import ReplanReason
from redteam_core.runtime.replan import evaluate_replan_events
from tests.integration.test_generic_step_loop import proposal, setup_engine


@pytest.mark.parametrize("event", list(ReplanReason))
def test_canonical_event_is_detected_once_without_model_call(tmp_path, event):
    _, session, runtime, candidate, driver, orchestrator = setup_engine(tmp_path, [])
    try:
        baseline = evaluate_replan_events(session, runtime, orchestrator.budget_limits)
        assert baseline.reasons == ()
        runtime.progress.replan_evaluations = (baseline,)
        runtime.progress.next_step_index = 2
        if event is ReplanReason.NEW_EVIDENCE:
            current = session.candidate_state.candidates[candidate.candidate_id]
            session.candidate_state.candidates[candidate.candidate_id] = current.model_copy(
                update={"evidence_ids": (*current.evidence_ids, "evidence:new")}
            )
        elif event is ReplanReason.DEPENDENCY_CONFLICT:
            req = next(iter(session.investigation.requirements))
            session.knowledge.slots[req] = KnowledgeSlot(
                requirement_id=req,
                status=KnowledgeSlotStatus.CONFLICTING,
                authoritative_payload_hashes=("a" * 64, "b" * 64),
            )
        elif event is ReplanReason.BLOCKED_ROOT:
            key, row = next(iter(session.candidate_state.coverage_ledger.items()))
            session.candidate_state.coverage_ledger[key] = row.model_copy(
                update={"disposition": CandidateDisposition.BLOCKED_EXTERNAL}
            )
        else:
            runtime.usage.requests = 10
        changed = evaluate_replan_events(session, runtime, orchestrator.budget_limits)
        assert changed.reasons == (event,)
        runtime.progress.replan_evaluations = (baseline, changed)
        runtime.progress.next_step_index = 3
        assert evaluate_replan_events(session, runtime, orchestrator.budget_limits).reasons == ()
        assert driver.calls == 0
    finally:
        orchestrator.close()


@pytest.mark.asyncio
async def test_event_feedback_uses_next_regular_request_and_survives_checkpoint(tmp_path):
    engine, session, runtime, candidate, driver, orchestrator = setup_engine(tmp_path, [])
    try:
        baseline = evaluate_replan_events(session, runtime, orchestrator.budget_limits)
        # A previous baseline is sufficient to test a canonical budget-drop event.
        runtime.progress.replan_evaluations = (baseline,)
        runtime.progress.next_step_index = 2
        runtime.usage.requests = 10
        driver.payloads.append(proposal(session, DecisionAction.FINISH, finish_reason="assess"))
        await engine.run_step()
        assert driver.calls == 1
        assert "BUDGET_DROP" in driver.feedback[0]
        assert runtime.progress.replan_evaluations[-1].reasons == (ReplanReason.BUDGET_DROP,)
        loaded = orchestrator.checkpoint_store.load(
            catalog=orchestrator.catalog,
            composition_fingerprint=orchestrator.composition_fingerprint,
            journal=orchestrator.journal,
        )
        assert (
            loaded.runtime_state.progress.replan_evaluations == runtime.progress.replan_evaluations
        )
    finally:
        orchestrator.close()
