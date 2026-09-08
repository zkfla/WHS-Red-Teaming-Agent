import pytest

from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    CleanupRequirement,
    CompletionStatus,
    CreatedResource,
    ExperimentAttemptStatus,
    RootCauseRef,
    ScopeCompletion,
    SecurityCandidate,
    stable_id,
)
from redteam_core.runtime.completion import assess_completion
from tests.helpers import requirement
from tests.unit.test_runtime_checkpoint import NOW, MutableClock, _authorized_session


def terminal_session(disposition=CandidateDisposition.ACCEPTED):
    session = CoreSession()
    contract = ClaimContract.create(
        claim_type_id="reference.candidate.completion@1",
        title="Completion candidate",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    candidate = SecurityCandidate.create(
        candidate_type_id="reference.candidate.completion@1",
        semantic_key="completion",
        root_cause=RootCauseRef(owner_bundle_id="reference", family_id="completion", key="one"),
        claim_contract=contract,
        max_claim_status=ClaimStatus.SUPPORTED,
        disposition=disposition,
        disposition_reason="terminal fixture",
    )
    reconcile_candidate_state(session, (candidate,))
    return session


def test_complete_requires_explicit_scope_and_nonblocked_terminal_root():
    result = assess_completion(
        terminal_session(),
        scope=ScopeCompletion(acquisition_complete=True, search_complete=True),
    )
    assert result.status is CompletionStatus.COMPLETE and not result.reasons


@pytest.mark.parametrize("started", [False, True])
def test_unfinished_authorized_experiment_prevents_complete(started):
    experiment_session, _, _ = _authorized_session(MutableClock(NOW))
    session = terminal_session()
    execution = experiment_session.execution.model_copy(deep=True)
    attempt = next(iter(execution.attempts.values()))
    if started:
        execution.attempts[attempt.attempt_id] = attempt.model_copy(
            update={
                "status": ExperimentAttemptStatus.STARTED,
                "started_at": NOW,
            }
        )
    session.execution = execution
    result = assess_completion(
        session,
        scope=ScopeCompletion(acquisition_complete=True, search_complete=True),
    )
    assert result.status is CompletionStatus.PARTIAL
    assert result.pending_experiment_ids == (attempt.attempt_id,)
    assert "PENDING_EXPERIMENTS" in result.reasons


def test_empty_candidates_are_not_a_clean_result_without_positive_scope_proof():
    result = assess_completion(
        CoreSession(),
        scope=ScopeCompletion(acquisition_complete=True, search_complete=True),
    )
    assert result.status is CompletionStatus.PARTIAL
    assert result.reasons == ("EMPTY_CANDIDATE_SET_UNPROVEN",)


def test_truncation_unresolved_requirement_and_blocked_root_remain_partial():
    session = terminal_session(CandidateDisposition.BLOCKED_EXTERNAL)
    req = requirement()
    session.investigation.requirements[req.requirement_id] = req
    result = assess_completion(
        session,
        scope=ScopeCompletion(
            acquisition_complete=True,
            search_complete=True,
            search_truncated=True,
        ),
    )
    assert result.status is CompletionStatus.PARTIAL
    assert set(result.reasons) == {
        "SEARCH_SCOPE_INCOMPLETE",
        "UNRESOLVED_REQUIREMENTS",
        "BLOCKED_EXTERNAL_ROOTS",
    }
    assert result.unresolved_requirement_ids == (req.requirement_id,)
    assert result.blocked_root_keys == (
        next(iter(session.candidate_state.coverage_ledger.values())).root_cause_key,
    )


def test_uncertain_effect_and_unfinished_cleanup_are_explicit_gaps():
    session, _descriptor, _policy = _authorized_session(MutableClock(NOW))
    attempt = next(iter(session.execution.attempts.values()))
    uncertain = attempt.model_copy(
        update={
            "status": ExperimentAttemptStatus.UNCERTAIN,
            "started_at": NOW,
            "completed_at": NOW,
        }
    )
    resource = CreatedResource(
        created_resource_id=stable_id(
            "created", "reference.resource.value@1", "resource:g7", uncertain.attempt_id
        ),
        resource_id="resource:g7",
        resource_type_id="reference.resource.value@1",
        created_by_attempt_id=uncertain.attempt_id,
    )
    cleanup = CleanupRequirement.for_resource(resource)
    execution = session.execution.model_copy(deep=True)
    execution.attempts[uncertain.attempt_id] = uncertain
    execution.created_resources[resource.created_resource_id] = resource
    execution.cleanup_requirements[cleanup.cleanup_requirement_id] = cleanup
    session.execution = execution.__class__.model_validate(execution.model_dump(mode="python"))
    result = assess_completion(
        session,
        scope=ScopeCompletion(acquisition_complete=True, search_complete=True),
    )
    assert "UNCERTAIN_EFFECTS" in result.reasons and "CLEANUP_GAPS" in result.reasons
    assert result.uncertain_effect_ids == (uncertain.attempt_id,)
    assert result.cleanup_gap_ids == (cleanup.cleanup_requirement_id,)
