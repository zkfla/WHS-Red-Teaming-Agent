from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from redteam_core.evidence import (
    EvidenceStorageViolation,
    RawArtifactPurpose,
    authorize_raw_artifact_use,
)
from redteam_core.knowledge import CoreSession, record_expectation
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    Evidence,
    EvidenceArtifactRef,
    EvidenceStoragePolicy,
    ExpectationRef,
    Provenance,
    Relation,
    RootCauseRef,
    SecurityCandidate,
    SensitivityClass,
    TriState,
    TrustClass,
    stable_id,
)
from redteam_core.models.base import canonical_json, sha256_text
from tests.helpers import SamplePayload, catalog

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_strict_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        Provenance(
            source_kind="test",
            producer_id="producer",
            trust_class=TrustClass.OBSERVED,
            acquired_at=NOW,
            content_hash="0" * 64,
            surprise=True,
        )


def test_tristate_preserves_unknown_as_first_class_value():
    assert {item.value for item in TriState} == {"TRUE", "FALSE", "UNKNOWN"}


def test_stable_id_is_deterministic_and_identity_sensitive():
    first = stable_id("rel", "a", "reference.relation.flows@1", "b")
    assert first == stable_id("rel", "a", "reference.relation.flows@1", "b")
    assert first != stable_id("rel", "a", "reference.relation.flows@1", "c")


def test_relation_identity_is_semantic_not_runtime_generated():
    one = Relation.create(
        source_ref="source:a",
        relation_type_id="reference.relation.flows@1",
        target_ref="sink:b",
        verdict=TriState.UNKNOWN,
    )
    two = Relation.create(
        source_ref="source:a",
        relation_type_id="reference.relation.flows@1",
        target_ref="sink:b",
        verdict=TriState.TRUE,
    )
    assert one.relation_id == two.relation_id


def test_evidence_binds_hash_and_raw_artifact_without_inlining_raw_content():
    content = "sensitive value"
    digest = sha256_text(content)
    provenance = Provenance(
        source_kind="fixture",
        producer_id="collector",
        trust_class=TrustClass.OBSERVED,
        acquired_at=NOW,
        content_hash=digest,
    )
    artifact = EvidenceArtifactRef(
        artifact_id="artifact:1",
        content_hash=digest,
        size_bytes=len(content),
        media_type="text/plain",
        storage_ref="artifact-store://1",
    )
    evidence = Evidence.from_content(
        evidence_type_id="reference.evidence.value@1",
        content=content,
        provenance=provenance,
        redacted_preview="[REDACTED]",
        sensitivity_class=SensitivityClass.SECRET,
        raw_artifact_ref=artifact,
    )
    dumped = evidence.model_dump_json()
    assert content not in dumped
    assert evidence.content_hash == digest


def test_evidence_storage_policy_defaults_fail_closed_for_raw_secret():
    policy = EvidenceStoragePolicy()
    assert not policy.allows_raw(sensitivity=SensitivityClass.SECRET, size_bytes=1)
    assert not policy.llm_raw_allowed


def test_raw_evidence_storage_boundary_enforces_retain_export_and_llm_policy():
    content = "raw internal material"
    digest = sha256_text(content)
    evidence = Evidence.from_content(
        evidence_type_id="reference.evidence.value@1",
        content=content,
        provenance=Provenance(
            source_kind="fixture",
            producer_id="collector",
            trust_class=TrustClass.OBSERVED,
            acquired_at=NOW,
            content_hash=digest,
        ),
        sensitivity_class=SensitivityClass.INTERNAL,
        raw_artifact_ref=EvidenceArtifactRef(
            artifact_id="artifact:policy-test",
            content_hash=digest,
            size_bytes=len(content),
            media_type="text/plain",
            storage_ref="artifact-store://policy-test",
        ),
    )

    with pytest.raises(EvidenceStorageViolation):
        authorize_raw_artifact_use(
            evidence,
            policy=EvidenceStoragePolicy(),
            purpose=RawArtifactPurpose.RETAIN,
        )

    retain_only = EvidenceStoragePolicy(
        raw_storage_allowed=True,
        max_artifact_bytes=10_000,
    )
    assert authorize_raw_artifact_use(
        evidence,
        policy=retain_only,
        purpose=RawArtifactPurpose.RETAIN,
    ).artifact_id == "artifact:policy-test"
    with pytest.raises(EvidenceStorageViolation):
        authorize_raw_artifact_use(
            evidence,
            policy=retain_only,
            purpose=RawArtifactPurpose.EXPORT,
        )
    with pytest.raises(EvidenceStorageViolation):
        authorize_raw_artifact_use(
            evidence,
            policy=retain_only,
            purpose=RawArtifactPurpose.LLM_CONTEXT,
        )

    fully_approved = retain_only.model_copy(
        update={"export_raw_allowed": True, "llm_raw_allowed": True}
    )
    assert authorize_raw_artifact_use(
        evidence,
        policy=fully_approved,
        purpose=RawArtifactPurpose.EXPORT,
    ) == evidence.raw_artifact_ref
    assert authorize_raw_artifact_use(
        evidence,
        policy=fully_approved,
        purpose=RawArtifactPurpose.LLM_CONTEXT,
    ) == evidence.raw_artifact_ref


def test_inferred_expectation_is_not_authoritative():
    cat = catalog()
    registration = cat.expectation_types.require("reference.expectation.value@1")
    envelope = registration.envelope_from_model(SamplePayload(value="expected"))
    expectation = ExpectationRef(
        expectation_id=stable_id(
            "exp",
            "reference.expectation.value@1",
            envelope.content_hash,
            TrustClass.INFERRED.value,
            "llm",
            "planner",
            "",
            canonical_json([]),
        ),
        expectation_type_id="reference.expectation.value@1",
        payload=envelope,
        provenance=Provenance(
            source_kind="llm",
            producer_id="planner",
            trust_class=TrustClass.INFERRED,
            acquired_at=NOW,
            content_hash=envelope.content_hash,
        ),
    )
    assert not expectation.authoritative


def test_expectation_ledger_preserves_inferred_authority_and_is_idempotent():
    cat = catalog()
    registration = cat.expectation_types.require("reference.expectation.value@1")
    envelope = registration.envelope_from_model(SamplePayload(value="expected"))
    expectation = ExpectationRef.create(
        expectation_type_id="reference.expectation.value@1",
        payload=envelope,
        provenance=Provenance(
            source_kind="llm",
            producer_id="planner",
            trust_class=TrustClass.INFERRED,
            acquired_at=NOW,
            content_hash=envelope.content_hash,
        ),
    )
    session = CoreSession()
    assert record_expectation(session, expectation, catalog=cat)
    assert not record_expectation(session, expectation, catalog=cat)
    assert session.expectations.revision == 1
    assert not session.expectations.expectations[expectation.expectation_id].authoritative


def test_expectation_ledger_accepts_registered_authoritative_baseline():
    cat = catalog()
    registration = cat.expectation_types.require("reference.expectation.value@1")
    envelope = registration.envelope_from_model(SamplePayload(value="expected"))
    expectation = ExpectationRef.create(
        expectation_type_id="reference.expectation.value@1",
        payload=envelope,
        provenance=Provenance(
            source_kind="baseline",
            producer_id="reference.baseline@1",
            trust_class=TrustClass.OBSERVED,
            acquired_at=NOW,
            content_hash=envelope.content_hash,
        ),
    )
    session = CoreSession()
    record_expectation(session, expectation, catalog=cat)
    assert session.expectations.expectations[expectation.expectation_id].authoritative


def test_expectation_payload_type_must_match_declared_expectation_type():
    cat = catalog()
    registration = cat.expectation_types.require("reference.expectation.value@1")
    envelope = registration.envelope_from_model(SamplePayload(value="expected"))
    provenance = Provenance(
        source_kind="baseline",
        producer_id="reference.baseline@1",
        trust_class=TrustClass.OBSERVED,
        acquired_at=NOW,
        content_hash=envelope.content_hash,
    )
    with pytest.raises(ValidationError):
        ExpectationRef(
            expectation_id=stable_id(
                "exp",
                "reference.expectation.other@1",
                envelope.content_hash,
                TrustClass.OBSERVED.value,
                "baseline",
                "reference.baseline@1",
                "",
                canonical_json([]),
            ),
            expectation_type_id="reference.expectation.other@1",
            payload=envelope,
            provenance=provenance,
        )


def test_typed_payload_envelope_is_immutable_after_registration_encoding():
    cat = catalog()
    envelope = cat.expectation_types.require("reference.expectation.value@1").envelope_from_model(
        SamplePayload(value="expected")
    )
    with pytest.raises(ValidationError):
        envelope.canonical_payload_json = "{}"


def test_candidate_cannot_exceed_claim_ceiling():
    contract = ClaimContract.create(
        claim_type_id="reference.candidate.value@1",
        title="Reference claim",
        max_claim_status=ClaimStatus.SUPPORTED,
    )
    root_cause = RootCauseRef(
        owner_bundle_id="reference",
        family_id="reference",
        key="root:key",
    )
    semantic_key = "occurrence:one"
    candidate_id = stable_id(
        "cand",
        "reference.candidate.value@1",
        root_cause.qualified_key,
        semantic_key,
    )
    with pytest.raises(ValidationError):
        SecurityCandidate(
            candidate_id=candidate_id,
            candidate_type_id="reference.candidate.value@1",
            semantic_key=semantic_key,
            root_cause=root_cause,
            claim_contract=contract,
            max_claim_status=ClaimStatus.SUPPORTED,
            current_claim_status=ClaimStatus.CONFIRMED,
            disposition=CandidateDisposition.INVESTIGATING,
        )
