from __future__ import annotations

import json

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text
from redteam_core.models.provenance import TYPE_ID_PATTERN


class DomainStateEnvelope(SchemaModel):
    """Opaque, bundle-owned state persisted by the generic runtime.

    Core persistence validates identity, canonical JSON shape and content hash only. It never
    interprets domain keys; encode/decode semantics belong to the registered domain codec.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    bundle_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*$")
    schema_version: str = Field(min_length=1, max_length=40)
    codec_id: str = Field(pattern=TYPE_ID_PATTERN)
    canonical_payload_json: str = Field(min_length=1, max_length=8_000_000)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> DomainStateEnvelope:
        try:
            decoded = json.loads(self.canonical_payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("domain state payload is not valid JSON") from exc
        canonical = canonical_json(decoded)
        if canonical != self.canonical_payload_json:
            raise ValueError("domain state payload must be canonical JSON")
        expected_hash = "sha256:" + sha256_text(self.canonical_payload_json)
        if self.payload_hash != expected_hash:
            raise ValueError("domain state payload hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        bundle_id: str,
        schema_version: str,
        codec_id: str,
        payload_json: str,
    ) -> DomainStateEnvelope:
        try:
            decoded = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("domain state codec emitted invalid JSON") from exc
        canonical = canonical_json(decoded)
        return cls(
            bundle_id=bundle_id,
            schema_version=schema_version,
            codec_id=codec_id,
            canonical_payload_json=canonical,
            payload_hash="sha256:" + sha256_text(canonical),
        )
