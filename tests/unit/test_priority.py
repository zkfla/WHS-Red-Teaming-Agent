from datetime import UTC, datetime, timedelta

import pytest

from redteam_core.investigation.coverage import (
    reconcile_candidate_state,
    select_candidate_portfolio,
)
from redteam_core.investigation.priority import (
    BaselinePriorityProvider,
    ExpectedUtilityPriorityProvider,
    prepare_priority_selection,
    root_evidence_fingerprint,
)
from redteam_core.investigation.resolution import ResolverRuntimeRegistry
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.priority import PriorityTerminalBinding
from redteam_core.models.runtime import RuntimeBudgetLimits, RuntimeState
from redteam_core.models.security import CandidateDisposition
from tests.unit.test_candidate_coverage import _candidate
from tests.unit.test_focused_context import fixture

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def pool(count=8):
    session = CoreSession()
    reconcile_candidate_state(session, tuple(_candidate(str(i % 3), str(i)) for i in range(count)))
    registry = ResolverRuntimeRegistry()
    registry.freeze()
    return session, RuntimeState(run_id="priority:test"), registry


def select(session, runtime, registry, *, provider=None, limit=3):
    return prepare_priority_selection(
        session,
        runtime,
        registry=registry,
        provider=provider or ExpectedUtilityPriorityProvider(),
        permissions=frozenset(),
        now=NOW,
        limits=RuntimeBudgetLimits(),
        limit=limit,
    )


def test_baseline_preserves_existing_portfolio_and_never_mutates_inputs():
    session, runtime, registry = pool()
    before = session.model_dump(mode="json"), runtime.model_dump(mode="json")
    expected = select_candidate_portfolio(session, limit=3)
    actual, state = select(session, runtime, registry, provider=BaselinePriorityProvider())
    assert actual.selected_candidate_ids == tuple(row.candidate_id for row in expected.entries)
    assert actual.fairness_bound_batches is None
    assert before == (session.model_dump(mode="json"), runtime.model_dump(mode="json"))
    assert state.batch_sequence == 1


def test_round_robin_floor_meets_stable_pool_bound_and_is_restart_deterministic():
    session, runtime, registry = pool()
    history = []
    for _ in range(16):
        selection, state = select(
            session, runtime, registry, provider=ExpectedUtilityPriorityProvider(max_wait_batches=8)
        )
        history.append(set(selection.selected_candidate_ids))
        assert selection.fairness_bound_batches == 8
        runtime.priority = state
        runtime = RuntimeState.model_validate_json(runtime.model_dump_json())
    expected = set(session.candidate_state.candidates)
    for index in range(9):
        assert set.union(*history[index : index + 8]) == expected
    assert all(row.selection_count >= 2 for row in runtime.priority.roots.values())


def test_infeasible_fairness_contract_fails_instead_of_silent_truncation():
    session, runtime, registry = pool()
    with pytest.raises(ValueError, match="infeasible fairness"):
        select(
            session, runtime, registry, provider=ExpectedUtilityPriorityProvider(max_wait_batches=7)
        )
    assert runtime.priority.batch_sequence == 0


def test_root_fingerprint_ignores_unrelated_root_and_candidate_identity_churn():
    session, runtime, registry = pool(2)
    candidate, unrelated = list(session.candidate_state.candidates.values())
    before = root_evidence_fingerprint(session, (candidate,), now=NOW)
    unrelated.evidence_ids.append("unrelated:evidence")
    assert root_evidence_fingerprint(session, (candidate,), now=NOW) == before
    renamed = candidate.model_copy(update={"candidate_id": "cand_" + "f" * 16})
    assert root_evidence_fingerprint(session, (renamed,), now=NOW) == before
    candidate.evidence_ids.append("relevant:evidence")
    assert root_evidence_fingerprint(session, (candidate,), now=NOW) != before


@pytest.mark.parametrize(
    "user_final,disposition,eligible",
    [
        (False, CandidateDisposition.REJECTED, True),
        (True, CandidateDisposition.REJECTED, False),
        (False, CandidateDisposition.MERGED, False),
    ],
)
def test_relevant_evidence_requests_review_but_preserves_locked_terminal(
    user_final,
    disposition,
    eligible,
):
    session, runtime, registry = pool(1)
    _, runtime.priority = select(session, runtime, registry)
    candidate = next(iter(session.candidate_state.candidates.values()))
    root = candidate.root_cause.qualified_key
    row = runtime.priority.roots[root]
    runtime.priority.roots[root] = row.model_copy(
        update={
            "terminal": PriorityTerminalBinding(
                disposition=disposition, evidence_hash=row.evidence_hash, user_final=user_final
            ),
        }
    )
    candidate.evidence_ids.append("changed:evidence")
    selection, state = select(session, runtime, registry)
    assert state.roots[root].reevaluation_required
    assert state.roots[root].terminal == runtime.priority.roots[root].terminal
    assert selection.assessments[0].eligible is eligible
    assert candidate.disposition is CandidateDisposition.UNREVIEWED


def test_legacy_terminal_has_unknown_binding_not_forged_current_evidence():
    session, runtime, registry = pool(1)
    candidate = next(iter(session.candidate_state.candidates.values()))
    candidate.disposition = CandidateDisposition.REJECTED
    session.candidate_state.coverage_ledger[
        candidate.candidate_id
    ].disposition = candidate.disposition
    _, state = select(session, runtime, registry)
    row = state.roots[candidate.root_cause.qualified_key]
    assert row.terminal.evidence_hash is None and row.reevaluation_required


def test_missing_resolver_is_explicitly_blocked_without_truth_change():
    session, candidate, _, _ = fixture()
    reconcile_candidate_state(session, (candidate,))
    session.investigation.observations.clear()
    _, runtime, registry = pool(1)
    result, _ = select(session, runtime, registry)
    assert result.selected_candidate_ids == ()
    assert result.assessments[0].blocked_reasons == ("NO_PERMITTED_PASSIVE_RESOLVER",)


def test_fresh_and_expired_evidence_have_different_root_binding():
    session, candidate, req, _ = fixture()
    from redteam_core.models.requirements import ObservationValidity

    obs_id, obs = next(iter(session.investigation.observations.items()))
    session.investigation.observations[obs_id] = obs.model_copy(
        update={
            "validity": ObservationValidity(not_after=NOW + timedelta(seconds=1)),
        }
    )
    assert root_evidence_fingerprint(session, (candidate,), now=NOW) != (
        root_evidence_fingerprint(session, (candidate,), now=NOW + timedelta(seconds=2))
    )
