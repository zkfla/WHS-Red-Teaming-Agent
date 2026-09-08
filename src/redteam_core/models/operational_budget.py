"""Typed operational request ledger. No I/O or request authorization by itself."""

from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    RELEASED = "RELEASED"


class ScopeRequestLimit(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)
    scope_id: str = Field(min_length=1, max_length=200)
    max_requests: int = Field(ge=0)


class OperationalBudgetLimits(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)
    max_requests: int = Field(default=1000, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    max_concurrent: int = Field(default=1, ge=1, le=1000)
    provider_limits: tuple[ScopeRequestLimit, ...] = Field(default=(), max_length=256)
    resolver_limits: tuple[ScopeRequestLimit, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def unique_scopes(self):
        for limits in (self.provider_limits, self.resolver_limits):
            if len({item.scope_id for item in limits}) != len(limits):
                raise ValueError("duplicate operational budget scope")
        return self


class RequestReservation(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)
    attempt_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=200)
    resolver_id: str = Field(min_length=1, max_length=200)
    reserved_tokens: int = Field(ge=0)
    token_bound_guaranteed: bool
    reserved_receipt_bytes: int = Field(default=1_048_576, ge=4096, le=1_048_576)
    actual_tokens: int | None = Field(default=None, ge=0)
    status: ReservationStatus = ReservationStatus.RESERVED

    @model_validator(mode="after")
    def final_usage_only(self):
        if self.actual_tokens is not None and self.status not in {
            ReservationStatus.SUCCEEDED,
            ReservationStatus.FAILED,
        }:
            raise ValueError("actual usage requires a terminal request result")
        return self


class OperationalBudgetState(SchemaModel):
    runtime_limits_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)
    run_id: str = Field(min_length=1, max_length=200)
    composition_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision: int = Field(default=0, ge=0)
    limits: OperationalBudgetLimits
    reservations: tuple[RequestReservation, ...] = Field(default=(), max_length=100_000)

    @model_validator(mode="after")
    def unique_attempts(self):
        if len({r.attempt_id for r in self.reservations}) != len(self.reservations):
            raise ValueError("duplicate operational attempt")
        return self

    @property
    def charged_requests(self) -> int:
        return sum(r.status is not ReservationStatus.RELEASED for r in self.reservations)

    @property
    def limits_hash(self) -> str:
        limits = self.limits.model_dump(mode="json")
        for key in ("provider_limits", "resolver_limits"):
            limits[key] = sorted(limits[key], key=lambda row: row["scope_id"])
        return "sha256:" + sha256_text(
            canonical_json({"operational": limits, "runtime": self.runtime_limits_hash})
        )

    @property
    def dispatched_requests(self) -> int:
        return sum(
            r.status not in {ReservationStatus.RESERVED, ReservationStatus.RELEASED}
            for r in self.reservations
        )

    @property
    def measured_tokens(self) -> int:
        return sum(r.actual_tokens or 0 for r in self.reservations)

    @property
    def charged_tokens(self) -> int:
        return sum(
            (r.actual_tokens if r.actual_tokens is not None else r.reserved_tokens)
            for r in self.reservations
            if r.status is not ReservationStatus.RELEASED
        )

    @property
    def active_requests(self) -> int:
        # UNCERTAIN may still be running remotely; do not free its concurrency slot.
        return sum(
            r.status
            in {
                ReservationStatus.RESERVED,
                ReservationStatus.DISPATCHED,
                ReservationStatus.UNCERTAIN,
            }
            for r in self.reservations
        )

    @property
    def token_bound_violated(self) -> bool:
        return any(
            r.token_bound_guaranteed
            and r.actual_tokens is not None
            and r.actual_tokens > r.reserved_tokens
            for r in self.reservations
        )
