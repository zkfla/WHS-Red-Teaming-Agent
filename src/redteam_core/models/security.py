from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from redteam_core.models.base import SchemaModel, TriState, canonical_json, stable_id
from redteam_core.models.provenance import TYPE_ID_PATTERN, Provenance, TypedPayloadEnvelope


class MissingReasonKind(StrEnum):
    LOCAL_RESOLVABLE = "LOCAL_RESOLVABLE"
    EXTERNAL_REQUIRED = "EXTERNAL_REQUIRED"
    UNMODELED = "UNMODELED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"


class MissingReason(SchemaModel):
    reason_id: str = Field(pattern=r"^miss_[0-9a-f]{16}$")
    kind: MissingReasonKind
    subject_ref: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=800)
    requirement_ids: list[str] = Field(default_factory=list, max_length=16)

    @classmethod
    def create(
        cls,
        *,
        kind: MissingReasonKind,
        subject_ref: str,
        description: str,
        requirement_ids: list[str] | None = None,
    ) -> MissingReason:
        return cls(
            reason_id=stable_id("miss", kind.value, subject_ref, description),
            kind=kind,
            subject_ref=subject_ref,
            description=description,
            requirement_ids=requirement_ids or [],
        )


class Relation(SchemaModel):
    relation_id: str = Field(pattern=r"^rel_[0-9a-f]{16}$")
    source_ref: str = Field(min_length=1, max_length=500)
    relation_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    target_ref: str = Field(min_length=1, max_length=500)
    verdict: TriState = TriState.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    missing_reason_ids: list[str] = Field(default_factory=list, max_length=64)
    requirement_ids: list[str] = Field(default_factory=list, max_length=64)
    context_refs: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_identity(self) -> Relation:
        expected = stable_id("rel", self.source_ref, self.relation_type_id, self.target_ref)
        if self.relation_id != expected:
            raise ValueError("relation_id does not match canonical relation identity")
        return self

    @classmethod
    def create(
        cls,
        *,
        source_ref: str,
        relation_type_id: str,
        target_ref: str,
        verdict: TriState = TriState.UNKNOWN,
        **kwargs: Any,
    ) -> Relation:
        return cls(
            relation_id=stable_id("rel", source_ref, relation_type_id, target_ref),
            source_ref=source_ref,
            relation_type_id=relation_type_id,
            target_ref=target_ref,
            verdict=verdict,
            **kwargs,
        )


class ExpectationRef(SchemaModel):
    expectation_id: str = Field(pattern=r"^exp_[0-9a-f]{16}$")
    expectation_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    payload: TypedPayloadEnvelope
    provenance: Provenance
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)

    def _expected_id(self) -> str:
        return stable_id(
            "exp",
            self.expectation_type_id,
            self.payload.content_hash,
            self.provenance.trust_class.value,
            self.provenance.source_kind,
            self.provenance.producer_id,
            self.provenance.location_ref or "",
            canonical_json(sorted(self.evidence_ids)),
        )

    @model_validator(mode="after")
    def validate_type_and_hash_binding(self) -> ExpectationRef:
        if self.payload.type_id != self.expectation_type_id:
            raise ValueError("expectation payload type does not match expectation_type_id")
        if self.payload.content_hash != self.provenance.content_hash:
            raise ValueError("expectation provenance must hash the expectation payload")
        if self.expectation_id != self._expected_id():
            raise ValueError("expectation_id does not match canonical expectation identity")
        return self

    @classmethod
    def create(
        cls,
        *,
        expectation_type_id: str,
        payload: TypedPayloadEnvelope,
        provenance: Provenance,
        evidence_ids: list[str] | None = None,
    ) -> ExpectationRef:
        evidence = sorted(set(evidence_ids or []))
        expectation_id = stable_id(
            "exp",
            expectation_type_id,
            payload.content_hash,
            provenance.trust_class.value,
            provenance.source_kind,
            provenance.producer_id,
            provenance.location_ref or "",
            canonical_json(evidence),
        )
        return cls(
            expectation_id=expectation_id,
            expectation_type_id=expectation_type_id,
            payload=payload,
            provenance=provenance,
            evidence_ids=evidence,
        )

    @property
    def authoritative(self) -> bool:
        return self.provenance.trust_class.authoritative


class ClaimStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    SUPPORTED = "SUPPORTED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


_CLAIM_RANK = {
    ClaimStatus.REJECTED: 0,
    ClaimStatus.CANDIDATE: 1,
    ClaimStatus.SUPPORTED: 2,
    ClaimStatus.CONFIRMED: 3,
}


class CandidateDisposition(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    INVESTIGATING = "INVESTIGATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    MERGED = "MERGED"


class RootCauseRef(SchemaModel):
    owner_bundle_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*$",
    )
    family_id: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=500)

    @property
    def qualified_key(self) -> str:
        return stable_id("root", self.owner_bundle_id, self.family_id, self.key)


class ClaimContract(SchemaModel):
    contract_id: str = Field(pattern=r"^contract_[0-9a-f]{16}$")
    claim_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    max_claim_status: ClaimStatus = ClaimStatus.SUPPORTED
    forbidden_claims: list[str] = Field(default_factory=list, max_length=64)

    @classmethod
    def create(
        cls,
        *,
        claim_type_id: str,
        title: str,
        max_claim_status: ClaimStatus = ClaimStatus.SUPPORTED,
        forbidden_claims: list[str] | None = None,
    ) -> ClaimContract:
        return cls(
            contract_id=stable_id("contract", claim_type_id, title, max_claim_status.value),
            claim_type_id=claim_type_id,
            title=title,
            max_claim_status=max_claim_status,
            forbidden_claims=forbidden_claims or [],
        )


class SecurityCandidate(SchemaModel):
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    candidate_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    semantic_key: str = Field(min_length=1, max_length=500)
    root_cause: RootCauseRef
    claim_contract: ClaimContract
    max_claim_status: ClaimStatus
    current_claim_status: ClaimStatus = ClaimStatus.CANDIDATE
    disposition: CandidateDisposition = CandidateDisposition.UNREVIEWED
    disposition_reason: str | None = Field(default=None, max_length=1_000)
    candidate_payload: TypedPayloadEnvelope | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    supporting_relation_ids: list[str] = Field(default_factory=list, max_length=128)
    contradicting_relation_ids: list[str] = Field(default_factory=list, max_length=128)
    guard_relation_ids: list[str] = Field(default_factory=list, max_length=64)
    requirement_ids: list[str] = Field(default_factory=list, max_length=64)
    grounding_refs: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_ceiling(self) -> SecurityCandidate:
        contract_max = self.claim_contract.max_claim_status
        if _CLAIM_RANK[self.max_claim_status] > _CLAIM_RANK[contract_max]:
            raise ValueError("candidate max_claim_status exceeds claim contract ceiling")
        if self.current_claim_status is not ClaimStatus.REJECTED and (
            _CLAIM_RANK[self.current_claim_status] > _CLAIM_RANK[self.max_claim_status]
        ):
            raise ValueError("candidate current_claim_status exceeds candidate ceiling")
        expected_id = stable_id(
            "cand",
            self.candidate_type_id,
            self.root_cause.qualified_key,
            self.semantic_key,
        )
        if self.candidate_id != expected_id:
            raise ValueError("candidate_id does not match canonical candidate identity")
        if self.candidate_payload is not None and (
            self.candidate_payload.type_id != self.candidate_type_id
        ):
            raise ValueError("candidate payload type does not match candidate_type_id")
        return self

    @property
    def root_cause_family(self) -> str:
        return self.root_cause.family_id

    @property
    def root_cause_key(self) -> str:
        return self.root_cause.key

    @classmethod
    def create(
        cls,
        *,
        candidate_type_id: str,
        semantic_key: str,
        root_cause: RootCauseRef,
        claim_contract: ClaimContract,
        max_claim_status: ClaimStatus,
        **kwargs: Any,
    ) -> SecurityCandidate:
        return cls(
            candidate_id=stable_id(
                "cand", candidate_type_id, root_cause.qualified_key, semantic_key
            ),
            candidate_type_id=candidate_type_id,
            semantic_key=semantic_key,
            root_cause=root_cause,
            claim_contract=claim_contract,
            max_claim_status=max_claim_status,
            **kwargs,
        )


class ValidationVerdict(StrEnum):
    PASS = "PASS"
    VIOLATION = "VIOLATION"
    ANOMALY = "ANOMALY"
    UNKNOWN = "UNKNOWN"


class ValidationOutcome(SchemaModel):
    outcome_id: str = Field(pattern=r"^out_[0-9a-f]{16}$")
    validator_id: str = Field(min_length=1, max_length=200)
    verdict: ValidationVerdict
    invariant_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1_200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    experiment_result_id: str | None = Field(default=None, pattern=r"^result_[0-9a-f]{16}$")
    knowledge_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> ValidationOutcome:
        expected = stable_id(
            "out",
            self.validator_id,
            self.invariant_id,
            self.candidate_id,
            self.experiment_result_id or "",
            self.knowledge_revision,
            self.candidate_revision,
            self.verdict.value,
            self.reason,
            canonical_json(sorted(self.evidence_ids)),
        )
        if self.outcome_id != expected:
            raise ValueError("outcome_id does not match canonical validation outcome")
        return self

    @classmethod
    def create(
        cls,
        *,
        validator_id: str,
        verdict: ValidationVerdict,
        invariant_id: str,
        reason: str,
        candidate_id: str,
        knowledge_revision: int,
        candidate_revision: int,
        evidence_ids: list[str] | None = None,
        experiment_result_id: str | None = None,
    ) -> ValidationOutcome:
        evidence = sorted(set(evidence_ids or []))
        return cls(
            outcome_id=stable_id(
                "out",
                validator_id,
                invariant_id,
                candidate_id,
                experiment_result_id or "",
                knowledge_revision,
                candidate_revision,
                verdict.value,
                reason,
                canonical_json(evidence),
            ),
            validator_id=validator_id,
            verdict=verdict,
            invariant_id=invariant_id,
            reason=reason,
            evidence_ids=evidence,
            candidate_id=candidate_id,
            experiment_result_id=experiment_result_id,
            knowledge_revision=knowledge_revision,
            candidate_revision=candidate_revision,
        )


class Finding(SchemaModel):
    finding_id: str = Field(pattern=r"^finding_[0-9a-f]{16}$")
    finding_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    candidate_id: str
    source_outcome_ids: list[str] = Field(min_length=1, max_length=64)
    status: ClaimStatus
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1_500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_identity(self) -> Finding:
        expected = stable_id(
            "finding",
            self.finding_type_id,
            self.candidate_id,
            canonical_json(sorted(self.source_outcome_ids)),
            self.status.value,
            self.title,
            self.summary,
        )
        if self.finding_id != expected:
            raise ValueError("finding_id does not match canonical materialization")
        return self

    @classmethod
    def create(
        cls,
        *,
        finding_type_id: str,
        candidate_id: str,
        source_outcome_ids: list[str],
        status: ClaimStatus,
        title: str,
        summary: str,
        evidence_ids: list[str] | None = None,
    ) -> Finding:
        outcomes = sorted(set(source_outcome_ids))
        evidence = sorted(set(evidence_ids or []))
        return cls(
            finding_id=stable_id(
                "finding",
                finding_type_id,
                candidate_id,
                canonical_json(outcomes),
                status.value,
                title,
                summary,
            ),
            finding_type_id=finding_type_id,
            candidate_id=candidate_id,
            source_outcome_ids=outcomes,
            status=status,
            title=title,
            summary=summary,
            evidence_ids=evidence,
        )
