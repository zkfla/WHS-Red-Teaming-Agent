from __future__ import annotations

from dataclasses import dataclass

import pytest

from redteam_core.investigation import (
    CandidateDeriverDescriptor,
    CandidateDeriverRegistry,
    RootCauseDecision,
    commit_root_cause_decisions,
    coverage_complete,
    derive_candidates,
    reconcile_candidate_state,
    record_portfolio_selection,
    select_candidate_portfolio,
)
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    Relation,
    RootCauseRef,
    SecurityCandidate,
)
from tests.helpers import catalog

CANDIDATE_TYPE = "reference.candidate.value@1"
RELATION_TYPE = "reference.relation.flows@1"


def _candidate(
    family: str,
    key: str,
    *,
    semantic_key: str | None = None,
    relation_ids: tuple[str, ...] = (),
    status: ClaimStatus = ClaimStatus.SUPPORTED,
) -> SecurityCandidate:
    contract = ClaimContract.create(
        claim_type_id=CANDIDATE_TYPE,
        title="Reference candidate",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    return SecurityCandidate.create(
        candidate_type_id=CANDIDATE_TYPE,
        semantic_key=semantic_key or f"occurrence:{family}:{key}",
        root_cause=RootCauseRef(
            owner_bundle_id="reference",
            family_id=family,
            key=key,
        ),
        claim_contract=contract,
        max_claim_status=ClaimStatus.SUPPORTED,
        current_claim_status=status,
        supporting_relation_ids=list(relation_ids),
    )


@dataclass
class DirectDeriver:
    descriptor: CandidateDeriverDescriptor
    candidates: tuple[SecurityCandidate, ...]

    def derive(self, session, extension_catalog):
        assert extension_catalog.frozen
        return self.candidates


def test_candidate_is_derived_directly_from_canonical_relations_without_finding_materialization():
    relation = Relation.create(
        source_ref="source:a",
        relation_type_id=RELATION_TYPE,
        target_ref="sink:b",
    )
    session = CoreSession()
    session.semantic.relations[relation.relation_id] = relation
    candidate = _candidate("flow", "root:one", relation_ids=(relation.relation_id,))
    registry = CandidateDeriverRegistry()
    registry.register(
        DirectDeriver(
            CandidateDeriverDescriptor(
                deriver_id="reference.deriver.direct@1",
            ),
            (candidate,),
        )
    )
    registry.freeze()

    derived = derive_candidates(session, catalog=catalog(), registry=registry)
    assert derived == (candidate,)
    assert derived[0].supporting_relation_ids == [relation.relation_id]


def test_pathless_candidate_is_valid_when_grounded_by_non_path_state_ref():
    session = CoreSession()
    candidate = _candidate("state-invariant", "root:pathless").model_copy(
        update={"grounding_refs": ["state:baseline-delta"]}
    )
    registry = CandidateDeriverRegistry()
    registry.register(
        DirectDeriver(
            CandidateDeriverDescriptor(deriver_id="reference.deriver.direct@1"),
            (candidate,),
        )
    )
    registry.freeze()
    derived = derive_candidates(session, catalog=catalog(), registry=registry)
    assert derived[0].supporting_relation_ids == []
    assert derived[0].grounding_refs == ["state:baseline-delta"]


def test_candidate_derivation_rejects_unknown_grounding_relation():
    session = CoreSession()
    candidate = _candidate(
        "flow",
        "root:one",
        relation_ids=("rel_0000000000000000",),
    )
    registry = CandidateDeriverRegistry()
    registry.register(
        DirectDeriver(
            CandidateDeriverDescriptor(
                deriver_id="reference.deriver.direct@1",
            ),
            (candidate,),
        )
    )
    registry.freeze()
    with pytest.raises(ValueError, match="unknown relation"):
        derive_candidates(session, catalog=catalog(), registry=registry)


def test_portfolio_prefers_root_cause_family_diversity_before_same_family_repeats():
    session = CoreSession()
    candidates = tuple(
        [
            _candidate("family-a", f"a-{index}")
            for index in range(4)
        ]
        + [_candidate("family-b", "b-0"), _candidate("family-c", "c-0")]
    )
    reconcile_candidate_state(session, candidates)
    portfolio = select_candidate_portfolio(session, limit=3)
    assert len(portfolio.entries) == 3
    assert {entry.root_cause_family for entry in portfolio.entries} == {
        "reference:family-a",
        "reference:family-b",
        "reference:family-c",
    }


def test_multi_batch_rotation_drains_more_roots_than_portfolio_size():
    session = CoreSession()
    candidates = tuple(
        _candidate(f"family-{index % 3}", f"root-{index}") for index in range(9)
    )
    reconcile_candidate_state(session, candidates)
    selected_roots: set[str] = set()

    while not coverage_complete(session):
        portfolio = select_candidate_portfolio(session, limit=3)
        assert portfolio.entries
        record_portfolio_selection(session, portfolio)
        selected_roots.update(entry.root_cause_key for entry in portfolio.entries)
        decisions = tuple(
            RootCauseDecision(
                root_cause_key=entry.root_cause_key,
                disposition=CandidateDisposition.ACCEPTED,
                reason="terminal reference decision",
            )
            for entry in portfolio.entries
        )
        commit_root_cause_decisions(session, portfolio, decisions)

    assert len(selected_roots) == 9
    assert session.candidate_state.batch_sequence == 3
    assert all(
        item.disposition is CandidateDisposition.ACCEPTED
        for item in session.candidate_state.candidates.values()
    )
    assert all(
        item.disposition is CandidateDisposition.ACCEPTED
        for item in session.candidate_state.coverage_ledger.values()
        if item.active
    )


def test_one_root_decision_updates_all_candidate_variants_in_that_root():
    session = CoreSession()
    first = _candidate("family-a", "same-root", semantic_key="variant:one")
    second = _candidate("family-a", "same-root", semantic_key="variant:two")
    reconcile_candidate_state(session, (first, second))
    portfolio = select_candidate_portfolio(session, limit=1)
    assert len(portfolio.entries) == 1
    record_portfolio_selection(session, portfolio)
    commit_root_cause_decisions(
        session,
        portfolio,
        (
            RootCauseDecision(
                root_cause_key=portfolio.entries[0].root_cause_key,
                disposition=CandidateDisposition.REJECTED,
                reason="same root is terminal",
            ),
        ),
    )
    assert all(
        item.disposition is CandidateDisposition.REJECTED
        for item in session.candidate_state.candidates.values()
    )
    assert coverage_complete(session)


def test_disappeared_candidate_keeps_terminal_coverage_history():
    session = CoreSession()
    candidate = _candidate("family-a", "root-one")
    reconcile_candidate_state(session, (candidate,))
    reconcile_candidate_state(session, ())
    ledger = session.candidate_state.coverage_ledger[candidate.candidate_id]
    assert not ledger.active
    assert ledger.disposition is CandidateDisposition.BLOCKED_EXTERNAL
    assert "disappeared" in (ledger.reason or "")


def test_batch_commit_requires_exactly_one_terminal_decision_per_selected_root():
    session = CoreSession()
    reconcile_candidate_state(session, (_candidate("family-a", "root-one"),))
    portfolio = select_candidate_portfolio(session, limit=1)
    record_portfolio_selection(session, portfolio)
    with pytest.raises(ValueError, match="exactly one"):
        commit_root_cause_decisions(session, portfolio, ())


def test_portfolio_cannot_be_recorded_twice_before_terminal_decision():
    session = CoreSession()
    reconcile_candidate_state(session, (_candidate("family-a", "root-one"),))
    portfolio = select_candidate_portfolio(session, limit=1)
    record_portfolio_selection(session, portfolio)
    with pytest.raises(ValueError, match="already recorded"):
        record_portfolio_selection(session, portfolio)


def test_terminal_decision_requires_portfolio_to_be_recorded_first():
    session = CoreSession()
    reconcile_candidate_state(session, (_candidate("family-a", "root-one"),))
    portfolio = select_candidate_portfolio(session, limit=1)
    decision = RootCauseDecision(
        root_cause_key=portfolio.entries[0].root_cause_key,
        disposition=CandidateDisposition.ACCEPTED,
        reason="terminal reference decision",
    )
    with pytest.raises(ValueError, match="recorded before"):
        commit_root_cause_decisions(session, portfolio, (decision,))


def test_stale_portfolio_is_rejected_after_knowledge_revision_changes():
    session = CoreSession()
    reconcile_candidate_state(session, (_candidate("family-a", "root-one"),))
    portfolio = select_candidate_portfolio(session, limit=1)
    session.knowledge.revision += 1
    with pytest.raises(ValueError, match="knowledge revision is stale"):
        record_portfolio_selection(session, portfolio)


def test_runtime_deriver_set_must_match_frozen_semantic_manifest():
    registry = CandidateDeriverRegistry()
    registry.freeze()
    with pytest.raises(ValueError, match="do not match"):
        derive_candidates(CoreSession(), catalog=catalog(), registry=registry)
