from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redteam_core.investigation import (
    RootCauseDecision,
    commit_root_cause_decisions,
    reconcile_candidate_state,
    record_portfolio_selection,
    select_candidate_portfolio,
)
from redteam_core.knowledge import CoreSession, apply_observation
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    RootCauseRef,
    RuntimeState,
    SecurityCandidate,
)
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
    resume_checkpoint,
)
from redteam_core.runtime.journal import EventJournal, RuntimeEventType
from tests.helpers import catalog, observation, requirement

NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _candidate() -> SecurityCandidate:
    contract = ClaimContract.create(
        claim_type_id="reference.candidate.value@1",
        title="Batch E crash-cut candidate",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    return SecurityCandidate.create(
        candidate_type_id="reference.candidate.value@1",
        semantic_key="batch-e:root",
        root_cause=RootCauseRef(
            owner_bundle_id="reference",
            family_id="batch-e",
            key="root-one",
        ),
        claim_contract=contract,
        max_claim_status=ClaimStatus.SUPPORTED,
        current_claim_status=ClaimStatus.SUPPORTED,
    )


def test_batch_e_checkpoint_crash_cut_preserves_observation_idempotency(tmp_path):
    clock = MutableClock(NOW)
    cat = catalog()
    req = requirement()
    obs = observation(req=req, value="allow", acquired_at=NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-e", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    composition = build_runtime_composition_fingerprint(catalog=cat)

    session = CoreSession()
    session.investigation.requirements[req.requirement_id] = req
    runtime_state = RuntimeState.create("run-e")
    store.write(
        session=session,
        runtime_state=runtime_state,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="before_observation",
    )

    # Crash cut A: semantic commit happened only in memory and was not checkpointed.
    apply_observation(
        session,
        obs,
        catalog=cat,
        evaluation_time=NOW,
        expected_revision=0,
    )
    assert session.knowledge.revision == 1
    clock.value += timedelta(seconds=1)
    resumed_before = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
    )
    assert resumed_before.session.knowledge.revision == 0
    first_commit = apply_observation(
        resumed_before.session,
        obs,
        catalog=cat,
        evaluation_time=NOW,
        expected_revision=0,
    )
    assert first_commit.revision == 1

    # Crash cut B: committed state is checkpointed; replaying the identical observation is a no-op.
    store.write(
        session=resumed_before.session,
        runtime_state=resumed_before.runtime_state,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="after_observation",
    )
    clock.value += timedelta(seconds=1)
    resumed_after = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
    )
    duplicate = apply_observation(
        resumed_after.session,
        obs,
        catalog=cat,
        evaluation_time=NOW,
        expected_revision=1,
    )
    assert duplicate.revision == 1
    assert duplicate.semantic_changed is False
    assert len(resumed_after.session.investigation.observations) == 1


def test_batch_e_candidate_coverage_commit_is_atomic_across_checkpoint_cuts(tmp_path):
    clock = MutableClock(NOW)
    cat = catalog()
    composition = build_runtime_composition_fingerprint(catalog=cat)
    journal = EventJournal(tmp_path / "candidate-events.jsonl", run_id="run-c", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    store = AtomicCheckpointStore(tmp_path / "candidate-checkpoint.json")
    session = CoreSession()
    runtime_state = RuntimeState.create("run-c")
    store.write(
        session=session,
        runtime_state=runtime_state,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="before_candidate_batch",
    )

    candidate = _candidate()
    reconcile_candidate_state(session, (candidate,))
    portfolio = select_candidate_portfolio(session, limit=1)
    record_portfolio_selection(session, portfolio)
    commit_root_cause_decisions(
        session,
        portfolio,
        (
            RootCauseDecision(
                root_cause_key=portfolio.entries[0].root_cause_key,
                disposition=CandidateDisposition.ACCEPTED,
                reason="terminal batch decision",
            ),
        ),
    )
    assert session.candidate_state.batch_sequence == 1

    # Crash before checkpoint: no half-written candidate/coverage transaction survives.
    clock.value += timedelta(seconds=1)
    before = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
    )
    assert before.session.candidate_state.candidates == {}
    assert before.session.candidate_state.coverage_ledger == {}
    assert before.session.candidate_state.batch_sequence == 0

    # Recompute deterministically, checkpoint, and prove the whole terminal batch survives together.
    reconcile_candidate_state(before.session, (candidate,))
    portfolio = select_candidate_portfolio(before.session, limit=1)
    record_portfolio_selection(before.session, portfolio)
    commit_root_cause_decisions(
        before.session,
        portfolio,
        (
            RootCauseDecision(
                root_cause_key=portfolio.entries[0].root_cause_key,
                disposition=CandidateDisposition.ACCEPTED,
                reason="terminal batch decision",
            ),
        ),
    )
    store.write(
        session=before.session,
        runtime_state=before.runtime_state,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="after_candidate_batch",
    )
    clock.value += timedelta(seconds=1)
    after = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
    )
    restored = next(iter(after.session.candidate_state.candidates.values()))
    ledger = next(iter(after.session.candidate_state.coverage_ledger.values()))
    assert restored.disposition is CandidateDisposition.ACCEPTED
    assert ledger.disposition is CandidateDisposition.ACCEPTED
    assert ledger.selection_count == 1
    assert after.session.candidate_state.batch_sequence == 1
