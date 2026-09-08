"""Bounded admission records for passive resolution; no executable callbacks in storage."""

from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel
from redteam_core.models.requirements import EvidenceRequirement, ResolutionFailureKind


class ResolutionBatchItem(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    fallback_attempt_ids: tuple[str, ...] = Field(default=(), max_length=255)
    receipt_hashes: tuple[str, ...] = Field(default=(), max_length=256)
    requirement: EvidenceRequirement
    waiter_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    descriptor_hashes: tuple[str, ...] = Field(default=(), max_length=256)
    failure_kind: ResolutionFailureKind | None = None
    receipt_hash: str | None = None
    settled: bool = False

    @property
    def attempt_ids(self) -> tuple[str, ...]:
        return (self.attempt_id, *self.fallback_attempt_ids)

    @model_validator(mode="after")
    def validate_item(self) -> Self:
        if len(set(self.attempt_ids)) != len(self.attempt_ids):
            raise ValueError("duplicate fallback attempt")
        if self.fallback_attempt_ids and len(self.attempt_ids) != len(self.descriptor_hashes):
            raise ValueError("fallback chain must match admitted descriptor order")
        if len(set(self.receipt_hashes)) != len(self.receipt_hashes):
            raise ValueError("duplicate batch receipt history")
        if self.receipt_hash is not None and self.receipt_hash not in self.receipt_hashes:
            raise ValueError("selected receipt is absent from history")
        if len(set(self.waiter_ids)) != len(self.waiter_ids):
            raise ValueError("duplicate batch waiter")
        if self.receipt_hash is not None and not self.settled:
            raise ValueError("receipt binding requires settled batch item")
        if self.settled and self.receipt_hash is None and self.failure_kind is None:
            raise ValueError("settled batch item requires response or failure")
        return self


class ResolutionBatch(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    batch_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    composition_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    auth_context: str = Field(min_length=1, max_length=200)
    permissions: tuple[str, ...] = Field(default=(), max_length=256)
    items: tuple[ResolutionBatchItem, ...] = Field(min_length=1, max_length=256)
    concurrency: int = Field(default=1, ge=1, le=8)
    per_provider_concurrency: int = Field(default=1, ge=1, le=8)
    allow_fallback: bool = False
    max_serialized_bytes: int = Field(default=1_048_576, ge=4096, le=1_048_576)
    deadline_at: datetime
    complete: bool = False
    cancelled_waiter_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > self.max_serialized_bytes:
            raise ValueError("serialized batch exceeds byte budget")
        waiters = {w for i in self.items for w in i.waiter_ids}
        if (
            len(set(self.cancelled_waiter_ids)) != len(self.cancelled_waiter_ids)
            or set(self.cancelled_waiter_ids) - waiters
        ):
            raise ValueError("invalid cancelled batch waiter")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("batch deadline must be timezone-aware")
        for keys in (
            [i.item_id for i in self.items],
            [a for i in self.items for a in i.attempt_ids],
            [w for i in self.items for w in i.waiter_ids],
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate batch identity")
        if self.complete and not all(i.settled for i in self.items):
            raise ValueError("complete batch contains pending items")
        return self
