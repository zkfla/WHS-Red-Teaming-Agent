from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id

SHA256_PATTERN = r"^[0-9a-f]{64}$"
TYPE_ID_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+@[1-9][0-9]*$"


class TrustClass(StrEnum):
    OBSERVED = "OBSERVED"
    DECLARED_TRUSTED = "DECLARED_TRUSTED"
    SPEC_DERIVED = "SPEC_DERIVED"
    INFERRED = "INFERRED"

    @property
    def authoritative(self) -> bool:
        return self is not TrustClass.INFERRED


class SensitivityClass(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"
    SECRET = "SECRET"


class Provenance(SchemaModel):
    source_kind: str = Field(min_length=1, max_length=80)
    producer_id: str = Field(min_length=1, max_length=160)
    trust_class: TrustClass
    acquired_at: datetime
    content_hash: str = Field(pattern=SHA256_PATTERN)
    location_ref: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def require_timezone(self) -> Provenance:
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("provenance acquired_at must be timezone-aware")
        return self


class EvidenceArtifactRef(SchemaModel):
    artifact_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=120)
    storage_ref: str = Field(min_length=1, max_length=2_000)


class EvidenceStoragePolicy(SchemaModel):
    raw_storage_allowed: bool = False
    max_artifact_bytes: int = Field(default=0, ge=0, le=1_000_000_000)
    export_raw_allowed: bool = False
    llm_raw_allowed: bool = False
    raw_allowed_sensitivity: frozenset[SensitivityClass] = Field(
        default_factory=lambda: frozenset({SensitivityClass.PUBLIC, SensitivityClass.INTERNAL})
    )

    def allows_raw(self, *, sensitivity: SensitivityClass, size_bytes: int) -> bool:
        return bool(
            self.raw_storage_allowed
            and size_bytes <= self.max_artifact_bytes
            and sensitivity in self.raw_allowed_sensitivity
        )


class Evidence(SchemaModel):
    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{16}$")
    evidence_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    provenance: Provenance
    content_hash: str = Field(pattern=SHA256_PATTERN)
    location_ref: str | None = Field(default=None, max_length=1_000)
    redacted_preview: str = Field(default="", max_length=2_000)
    sensitivity_class: SensitivityClass = SensitivityClass.INTERNAL
    raw_artifact_ref: EvidenceArtifactRef | None = None

    @model_validator(mode="after")
    def validate_hash_binding(self) -> Evidence:
        if self.content_hash != self.provenance.content_hash:
            raise ValueError("evidence content_hash must match provenance content_hash")
        if self.raw_artifact_ref and self.raw_artifact_ref.content_hash != self.content_hash:
            raise ValueError("raw artifact content_hash must match evidence content_hash")
        return self

    @classmethod
    def from_content(
        cls,
        *,
        evidence_type_id: str,
        content: str,
        provenance: Provenance,
        location_ref: str | None = None,
        redacted_preview: str = "",
        sensitivity_class: SensitivityClass = SensitivityClass.INTERNAL,
        raw_artifact_ref: EvidenceArtifactRef | None = None,
    ) -> Evidence:
        content_hash = sha256_text(content)
        if provenance.content_hash != content_hash:
            raise ValueError("provenance does not describe supplied evidence content")
        return cls(
            evidence_id=stable_id("ev", evidence_type_id, content_hash, location_ref or ""),
            evidence_type_id=evidence_type_id,
            provenance=provenance,
            content_hash=content_hash,
            location_ref=location_ref,
            redacted_preview=redacted_preview,
            sensitivity_class=sensitivity_class,
            raw_artifact_ref=raw_artifact_ref,
        )


class TypedPayloadEnvelope(SchemaModel):
    """Opaque-to-core canonical payload validated by a registered Pydantic schema."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    type_id: str = Field(pattern=TYPE_ID_PATTERN)
    schema_version: str = Field(min_length=1, max_length=40)
    canonical_payload_json: str = Field(min_length=2, max_length=5_000_000)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_canonical_payload(self) -> TypedPayloadEnvelope:
        try:
            raw: Any = json.loads(self.canonical_payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("canonical_payload_json is not JSON") from exc
        normalized = canonical_json(raw)
        if normalized != self.canonical_payload_json:
            raise ValueError("payload JSON is not canonical")
        if sha256_text(normalized) != self.content_hash:
            raise ValueError("payload content_hash mismatch")
        return self
