"""Domain-neutral requirement and observation data contracts, without runtime dependencies."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from redteam_core.models.base import SchemaModel, stable_id
from redteam_core.models.provenance import TYPE_ID_PATTERN, Provenance, TypedPayloadEnvelope


class EvidenceFreshness(SchemaModel):
    max_age_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    required_snapshot_ref: str | None = Field(default=None, max_length=500)
    revalidate_on_resume: bool = False


class EvidenceRequirement(SchemaModel):
    requirement_id: str = Field(pattern=r"^req_[0-9a-f]{16}$")
    requirement_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    provider_id: str = Field(min_length=1, max_length=120)
    subject_ref: str = Field(min_length=1, max_length=500)
    qualifier: str | None = Field(default=None, max_length=500)
    basis_relation_ids: list[str] = Field(default_factory=list, max_length=64)
    investigation_item_ids: list[str] = Field(default_factory=list, max_length=32)
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    reason: str = Field(min_length=1, max_length=1_000)
    freshness: EvidenceFreshness = Field(default_factory=EvidenceFreshness)

    @model_validator(mode="after")
    def validate_identity(self) -> EvidenceRequirement:
        expected = stable_id(
            "req",
            self.requirement_type_id,
            self.provider_id,
            self.subject_ref,
            self.qualifier or "",
            self.freshness.max_age_seconds,
            self.freshness.required_snapshot_ref or "",
            self.freshness.revalidate_on_resume,
        )
        if self.requirement_id != expected:
            raise ValueError("requirement_id does not match canonical requirement identity")
        return self

    @classmethod
    def create(
        cls,
        *,
        requirement_type_id: str,
        provider_id: str,
        subject_ref: str,
        reason: str,
        qualifier: str | None = None,
        freshness: EvidenceFreshness | None = None,
        **kwargs,
    ) -> EvidenceRequirement:
        effective_freshness = freshness or EvidenceFreshness()
        return cls(
            requirement_id=stable_id(
                "req",
                requirement_type_id,
                provider_id,
                subject_ref,
                qualifier or "",
                effective_freshness.max_age_seconds,
                effective_freshness.required_snapshot_ref or "",
                effective_freshness.revalidate_on_resume,
            ),
            requirement_type_id=requirement_type_id,
            provider_id=provider_id,
            subject_ref=subject_ref,
            qualifier=qualifier,
            reason=reason,
            freshness=effective_freshness,
            **kwargs,
        )


class ObservationValidity(SchemaModel):
    snapshot_ref: str | None = Field(default=None, max_length=500)
    not_after: datetime | None = None

    @model_validator(mode="after")
    def validate_not_after(self) -> ObservationValidity:
        if self.not_after is not None and (
            self.not_after.tzinfo is None or self.not_after.utcoffset() is None
        ):
            raise ValueError("observation validity not_after must be timezone-aware")
        return self


class Observation(SchemaModel):
    observation_id: str = Field(pattern=r"^obs_[0-9a-f]{16}$")
    requirement_id: str = Field(pattern=r"^req_[0-9a-f]{16}$")
    resolver_id: str = Field(min_length=1, max_length=200)
    subject_ref: str = Field(min_length=1, max_length=500)
    payload: TypedPayloadEnvelope
    provenance: Provenance
    validity: ObservationValidity = Field(default_factory=ObservationValidity)
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_payload_provenance(self) -> Observation:
        if self.payload.content_hash != self.provenance.content_hash:
            raise ValueError("observation provenance must hash the observation payload")
        if (
            self.validity.not_after is not None
            and self.validity.not_after < self.provenance.acquired_at
        ):
            raise ValueError("observation validity cannot expire before acquisition")
        expected = stable_id(
            "obs",
            self.requirement_id,
            self.resolver_id,
            self.subject_ref,
            self.payload.content_hash,
            self.provenance.acquired_at.isoformat(),
            self.validity.snapshot_ref or "",
            self.validity.not_after.isoformat() if self.validity.not_after else "",
        )
        if self.observation_id != expected:
            raise ValueError("observation_id does not match canonical observation identity")
        return self

    @classmethod
    def create(
        cls,
        *,
        requirement_id: str,
        resolver_id: str,
        subject_ref: str,
        payload: TypedPayloadEnvelope,
        provenance: Provenance,
        validity: ObservationValidity | None = None,
        evidence_ids: list[str] | None = None,
    ) -> Observation:
        # An observation is an acquisition event, not a semantic entity. acquired_at is therefore
        # part of event identity so a later revalidation of identical bytes remains distinguishable.
        return cls(
            observation_id=stable_id(
                "obs",
                requirement_id,
                resolver_id,
                subject_ref,
                payload.content_hash,
                provenance.acquired_at.isoformat(),
                (validity.snapshot_ref if validity else "") or "",
                validity.not_after.isoformat() if validity and validity.not_after else "",
            ),
            requirement_id=requirement_id,
            resolver_id=resolver_id,
            subject_ref=subject_ref,
            payload=payload,
            provenance=provenance,
            validity=validity or ObservationValidity(),
            evidence_ids=evidence_ids or [],
        )


class ResolutionFailureKind(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    TRANSIENT = "TRANSIENT"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    AMBIGUOUS_NOT_FOUND = "AMBIGUOUS_NOT_FOUND"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"


class ResolutionFailureRecord(SchemaModel):
    failure_id: str = Field(pattern=r"^fail_[0-9a-f]{16}$")
    requirement_id: str = Field(pattern=r"^req_[0-9a-f]{16}$")
    resolver_id: str | None = Field(default=None, max_length=200)
    kind: ResolutionFailureKind
    detail: str = Field(min_length=1, max_length=1_500)
    retryable: bool = False
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> ResolutionFailureRecord:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("resolution failure time must be timezone-aware")
        return self

    @classmethod
    def create(
        cls,
        *,
        requirement_id: str,
        resolver_id: str | None,
        kind: ResolutionFailureKind,
        detail: str,
        retryable: bool,
        occurred_at: datetime,
    ) -> ResolutionFailureRecord:
        return cls(
            failure_id=stable_id(
                "fail",
                requirement_id,
                resolver_id or "",
                kind.value,
                occurred_at.isoformat(),
                detail,
            ),
            requirement_id=requirement_id,
            resolver_id=resolver_id,
            kind=kind,
            detail=detail,
            retryable=retryable,
            occurred_at=occurred_at,
        )
