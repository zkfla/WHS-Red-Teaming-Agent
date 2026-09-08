from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam_core.investigation import reconcile_candidate_state
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    Relation,
    RootCauseRef,
    SecurityCandidate,
    TriState,
    WorkingHypothesisStatus,
    WorkingHypothesisUpsert,
    WorkingMemoryPatch,
)
from redteam_core.reasoning import apply_working_memory_patch, reconcile_working_memory

RELATION_TYPE = "reference.relation.memory@1"
CANDIDATE_TYPE = "reference.candidate.value@1"


def _relation(*, verdict: TriState = TriState.TRUE, evidence: bool = True) -> Relation:
    return Relation.create(
        source_ref="source:x",
        relation_type_id=RELATION_TYPE,
        target_ref="target:y",
        verdict=verdict,
        evidence_ids=["ev_reference"] if evidence else [],
        missing_reason_ids=["miss_reference"] if verdict is TriState.UNKNOWN else [],
        requirement_ids=["req_reference"] if verdict is TriState.UNKNOWN else [],
    )


def _candidate(relation: Relation) -> SecurityCandidate:
    contract = ClaimContract.create(
        claim_type_id=CANDIDATE_TYPE,
        title="Reference memory claim",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    return SecurityCandidate.create(
        candidate_type_id=CANDIDATE_TYPE,
        semantic_key="memory:one",
        root_cause=RootCauseRef(
            owner_bundle_id="reference",
            family_id="memory",
            key="one",
        ),
        claim_contract=contract,
        max_claim_status=ClaimStatus.SUPPORTED,
        current_claim_status=ClaimStatus.SUPPORTED,
        supporting_relation_ids=[relation.relation_id],
        evidence_ids=list(relation.evidence_ids),
    )


def _session(
    *,
    verdict: TriState = TriState.TRUE,
    candidate: bool = False,
) -> tuple[CoreSession, Relation]:
    session = CoreSession()
    relation = _relation(verdict=verdict)
    session.semantic.relations[relation.relation_id] = relation
    if candidate:
        reconcile_candidate_state(session, (_candidate(relation),))
    return session, relation


def _patch(relation_id: str, *, expected_revision: int = 0) -> WorkingMemoryPatch:
    return WorkingMemoryPatch(
        expected_revision=expected_revision,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="reference:hypothesis:one",
                claim="The canonical relation may support the claim",
                supporting_relation_ids=(relation_id,),
                decision_summary="Investigate the canonical grounding.",
            ),
        ),
    )


def test_patch_derives_evidence_from_canonical_relation_and_never_accepts_direct_evidence():
    session, relation = _session()
    knowledge_before = session.knowledge.model_copy(deep=True)
    semantic_before = session.semantic.model_copy(deep=True)
    candidates_before = session.candidate_state.model_copy(deep=True)
    result = apply_working_memory_patch(session, _patch(relation.relation_id))
    assert result.accepted and result.changed
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    assert hypothesis.evidence_ids == ("ev_reference",)
    assert session.knowledge == knowledge_before
    assert session.semantic == semantic_before
    assert session.candidate_state == candidates_before
    with pytest.raises(ValidationError):
        WorkingHypothesisUpsert.model_validate(
            {
                "semantic_key": "reference:hypothesis:forged",
                "claim": "forged",
                "decision_summary": "forged",
                "evidence_ids": ["ev_forged"],
            }
        )


def test_working_memory_free_text_is_redacted_before_storage():
    session, relation = _session()
    patch = WorkingMemoryPatch(
        expected_revision=0,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="reference:hypothesis:redaction",
                claim="Investigate token=super-secret-value in the observed relation",
                supporting_relation_ids=(relation.relation_id,),
                decision_summary="authorization=very-secret-header must not be stored raw",
            ),
        ),
    )
    result = apply_working_memory_patch(session, patch)
    assert result.accepted
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    assert "super-secret-value" not in hypothesis.claim
    assert "very-secret-header" not in hypothesis.decision_summary
    assert "[REDACTED]" in hypothesis.claim
    assert "[REDACTED]" in hypothesis.decision_summary


def test_patch_rejects_unknown_grounding_without_mutation():
    session = CoreSession()
    result = apply_working_memory_patch(session, _patch("rel_0000000000000000"))
    assert not result.accepted
    assert session.working_memory.revision == 0
    assert not session.working_memory.hypotheses


def test_patch_rejects_stale_revision_and_semantic_noop_does_not_increment_revision():
    session, relation = _session()
    created = apply_working_memory_patch(session, _patch(relation.relation_id))
    assert created.accepted and session.working_memory.revision == 1
    stale = apply_working_memory_patch(session, _patch(relation.relation_id, expected_revision=0))
    assert not stale.accepted
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    noop = WorkingMemoryPatch(
        expected_revision=1,
        upserts=(
            WorkingHypothesisUpsert(
                hypothesis_id=hypothesis.hypothesis_id,
                decision_summary=hypothesis.decision_summary,
            ),
        ),
    )
    result = apply_working_memory_patch(session, noop)
    assert result.accepted and not result.changed
    assert session.working_memory.revision == 1


def test_ready_status_requires_supported_canonical_grounding():
    session, relation = _session(verdict=TriState.FALSE)
    patch = WorkingMemoryPatch(
        expected_revision=0,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="reference:hypothesis:ready",
                claim="This should not become ready",
                status=WorkingHypothesisStatus.READY_FOR_PROPOSAL,
                supporting_relation_ids=(relation.relation_id,),
                decision_summary="Attempt unsupported readiness.",
            ),
        ),
    )
    result = apply_working_memory_patch(session, patch)
    assert not result.accepted
    assert any("FALSE relations" in error for error in result.errors)


def test_candidate_binding_is_canonical_and_unrelated_relation_is_rejected():
    session, relation = _session(candidate=True)
    candidate = next(iter(session.candidate_state.candidates.values()))
    other = Relation.create(
        source_ref="source:other",
        relation_type_id=RELATION_TYPE,
        target_ref="target:other",
        verdict=TriState.TRUE,
    )
    session.semantic.relations[other.relation_id] = other
    patch = WorkingMemoryPatch(
        expected_revision=0,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="reference:hypothesis:candidate",
                candidate_id=candidate.candidate_id,
                claim="Candidate-bound claim",
                supporting_relation_ids=(other.relation_id,),
                decision_summary="This unrelated relation must be rejected.",
            ),
        ),
    )
    result = apply_working_memory_patch(session, patch)
    assert not result.accepted
    assert any("unrelated relations" in error for error in result.errors)
    assert relation.relation_id in candidate.supporting_relation_ids


def test_reconcile_discards_hypothesis_after_relation_turns_false_and_clears_focus():
    session, relation = _session()
    created = apply_working_memory_patch(session, _patch(relation.relation_id))
    assert created.accepted
    session.semantic.relations[relation.relation_id] = relation.model_copy(
        update={"verdict": TriState.FALSE}
    )
    result = reconcile_working_memory(session)
    assert result.changed
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    assert hypothesis.status is WorkingHypothesisStatus.DISCARDED
    assert session.working_memory.focus_hypothesis_id is None


def test_reconcile_marks_candidate_blocked_and_candidate_disappearance_discarded():
    session, relation = _session(candidate=True)
    candidate = next(iter(session.candidate_state.candidates.values()))
    patch = WorkingMemoryPatch(
        expected_revision=0,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="reference:hypothesis:candidate",
                candidate_id=candidate.candidate_id,
                claim="Candidate-bound claim",
                decision_summary="Track candidate recompute.",
            ),
        ),
    )
    assert apply_working_memory_patch(session, patch).accepted
    blocked = candidate.model_copy(
        update={
            "disposition": CandidateDisposition.BLOCKED_EXTERNAL,
            "disposition_reason": "external evidence required",
        }
    )
    reconcile_candidate_state(session, (blocked,))
    result = reconcile_working_memory(session)
    assert result.changed
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    assert hypothesis.status is WorkingHypothesisStatus.BLOCKED

    reconcile_candidate_state(session, ())
    result = reconcile_working_memory(session)
    assert result.changed
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    assert hypothesis.status is WorkingHypothesisStatus.DISCARDED


def test_working_memory_enforces_eight_hypothesis_limit_across_multiple_patches():
    session = CoreSession()
    relations: list[Relation] = []
    for index in range(9):
        relation = Relation.create(
            source_ref=f"source:{index}",
            relation_type_id=RELATION_TYPE,
            target_ref=f"target:{index}",
            verdict=TriState.TRUE,
            evidence_ids=[f"ev_{index:016x}"],
        )
        session.semantic.relations[relation.relation_id] = relation
        relations.append(relation)

    for index in range(8):
        patch = WorkingMemoryPatch(
            expected_revision=session.working_memory.revision,
            upserts=(
                WorkingHypothesisUpsert(
                    semantic_key=f"reference:hypothesis:{index}",
                    claim=f"Investigate relation {index}",
                    supporting_relation_ids=(relations[index].relation_id,),
                    decision_summary="Bounded hypothesis.",
                ),
            ),
        )
        assert apply_working_memory_patch(session, patch).accepted
    assert len(session.working_memory.hypotheses) == 8

    ninth = WorkingMemoryPatch(
        expected_revision=session.working_memory.revision,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="reference:hypothesis:overflow",
                claim="This ninth hypothesis must not be stored",
                supporting_relation_ids=(relations[8].relation_id,),
                decision_summary="Reject bounded overflow.",
            ),
        ),
    )
    result = apply_working_memory_patch(session, ninth)
    assert not result.accepted
    assert "hypothesis_limit_exceeded" in result.errors[0]
    assert len(session.working_memory.hypotheses) == 8


def test_reconcile_marker_only_update_does_not_increment_working_memory_revision():
    session, relation = _session()
    assert apply_working_memory_patch(session, _patch(relation.relation_id)).accepted
    first = reconcile_working_memory(session)
    # The TRUE evidenced relation legitimately changes ACTIVE -> READY on first reconciliation.
    assert first.changed
    revision = session.working_memory.revision
    session.knowledge.revision += 1
    second = reconcile_working_memory(session)
    assert not second.changed
    assert session.working_memory.revision == revision
    assert session.working_memory.last_reconciled_knowledge_revision == session.knowledge.revision
