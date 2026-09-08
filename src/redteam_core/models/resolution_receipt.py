"""Durable passive responses; a stored response is not yet canonical knowledge."""

from typing import Self

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text
from redteam_core.models.requirements import (
    EvidenceRequirement,
    Observation,
    ResolutionFailureRecord,
)

MAX_RESOLUTION_RECEIPTS = 10_000
MAX_RESOLUTION_RECEIPT_BYTES = 1_048_576
MAX_RESOLUTION_RECEIPT_LEDGER_BYTES = 67_108_864


class ResolutionReceipt(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    run_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    composition_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    limits_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    descriptor_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requirement: EvidenceRequirement
    observation: Observation | None = None
    failure: ResolutionFailureRecord | None = None
    serialized_bytes: int = Field(ge=1, le=MAX_RESOLUTION_RECEIPT_BYTES)

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if (self.observation is None) == (self.failure is None):
            raise ValueError("receipt requires exactly one response or failure")
        result = self.observation or self.failure
        if result.requirement_id != self.requirement.requirement_id:
            raise ValueError("receipt requirement binding mismatch")
        if self.observation and self.observation.subject_ref != self.requirement.subject_ref:
            raise ValueError("receipt subject binding mismatch")
        payload = self.model_dump(mode="json", exclude={"receipt_hash"})
        size_payload = self.model_dump(mode="json", exclude={"receipt_hash", "serialized_bytes"})
        actual_size = len(canonical_json(size_payload).encode("utf-8"))
        if self.serialized_bytes != actual_size:
            raise ValueError("receipt serialized byte count mismatch")
        if self.receipt_hash != "sha256:" + sha256_text(canonical_json(payload)):
            raise ValueError("receipt content hash mismatch")
        return self

    @classmethod
    def create(cls, **kwargs) -> Self:
        kwargs.pop("serialized_bytes", None)
        provisional = cls.model_construct(receipt_hash="", serialized_bytes=1, **kwargs)
        size_payload = provisional.model_dump(
            mode="json", exclude={"receipt_hash", "serialized_bytes"}
        )
        serialized_bytes = len(canonical_json(size_payload).encode("utf-8"))
        provisional = provisional.model_copy(update={"serialized_bytes": serialized_bytes})
        payload = provisional.model_dump(mode="json", exclude={"receipt_hash"})
        return cls.model_validate(
            {**payload, "receipt_hash": "sha256:" + sha256_text(canonical_json(payload))}
        )
