import pytest

from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.models.decision import ScopeCompletion
from redteam_core.models.security import CandidateDisposition, ClaimContract
from redteam_core.runtime.completion import assess_completion
from tests.integration.test_durable_priority import setup_priority
from tests.unit.test_candidate_coverage import _candidate
from tests.unit.test_priority import NOW


@pytest.mark.parametrize("user_final", [False, True])
def test_identical_root_evidence_survives_candidate_reidentification_without_deadlock(
    tmp_path, user_final
):
    coordinator, providers, _, _ = setup_priority(tmp_path, count=1)
    try:
        selected = coordinator.select(providers[1]).assessments[0]
        decision = coordinator.record_decision(
            root_key=selected.facts.root_key,
            expected_evidence_hash=selected.evidence_hash,
            disposition=CandidateDisposition.REJECTED,
            reason="durable synthetic negative decision",
            user_final=user_final,
        )
        original = next(iter(coordinator.session.candidate_state.candidates.values()))
        replacement = _candidate(
            original.root_cause.family_id,
            original.root_cause.key,
            semantic_key="replacement-occurrence",
        )
        assert replacement.candidate_id != original.candidate_id
        reconcile_candidate_state(coordinator.session, (replacement,))
        assert coordinator.session.candidate_state.candidates[
            replacement.candidate_id
        ].disposition is (CandidateDisposition.UNREVIEWED)
        coordinator.select(providers[1])
        assert coordinator.session.candidate_state.candidates[
            replacement.candidate_id
        ].disposition is (CandidateDisposition.REJECTED)
        assert coordinator.runtime.priority.decisions == (decision,)
        assessment = assess_completion(
            coordinator.session,
            scope=ScopeCompletion(acquisition_complete=True, search_complete=True),
            priority=coordinator.runtime.priority,
            evaluation_time=NOW,
        )
        assert assessment.status.value == "COMPLETE"
    finally:
        coordinator.orchestrator.close()


@pytest.mark.parametrize("change", ["evidence", "contract"])
def test_successor_with_changed_evidence_or_claim_contract_does_not_inherit_terminal(
    tmp_path, change
):
    coordinator, providers, _, _ = setup_priority(tmp_path, count=1)
    try:
        selected = coordinator.select(providers[1]).assessments[0]
        coordinator.record_decision(
            root_key=selected.facts.root_key,
            expected_evidence_hash=selected.evidence_hash,
            disposition=CandidateDisposition.REJECTED,
            reason="original negative decision",
        )
        original = next(iter(coordinator.session.candidate_state.candidates.values()))
        replacement = _candidate(
            original.root_cause.family_id,
            original.root_cause.key,
            semantic_key="replacement-occurrence",
        )
        if change == "evidence":
            replacement.evidence_ids.append("new:proof")
        else:
            replacement.claim_contract = ClaimContract.create(
                claim_type_id=replacement.claim_contract.claim_type_id,
                title="A different claim must not inherit the old decision",
            )
        reconcile_candidate_state(coordinator.session, (replacement,))
        selected_again = coordinator.select(providers[1])
        assert selected_again.assessments[0].reevaluation_required
        assert coordinator.session.candidate_state.candidates[
            replacement.candidate_id
        ].disposition is (CandidateDisposition.UNREVIEWED)
    finally:
        coordinator.orchestrator.close()
