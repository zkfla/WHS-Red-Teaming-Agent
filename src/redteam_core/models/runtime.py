from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id


class RuntimeLifecycle(StrEnum):
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    FINALIZED = "FINALIZED"


class TerminationKind(StrEnum):
    POLICY_STOP = "POLICY_STOP"
    BUDGET_STOP = "BUDGET_STOP"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    HANDLED_EXTERNAL_FAILURE = "HANDLED_EXTERNAL_FAILURE"
    NORMAL_COMPLETION = "NORMAL_COMPLETION"
    LLM_FINISH = "LLM_FINISH"


class FinalizationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    COMPLETE_WITH_CLEANUP_GAPS = "COMPLETE_WITH_CLEANUP_GAPS"


class RuntimeUsage(SchemaModel):
    rounds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    active_duration_seconds: float = Field(default=0.0, ge=0)

    def dominates(self, previous: RuntimeUsage) -> bool:
        return bool(
            self.rounds >= previous.rounds
            and self.tokens >= previous.tokens
            and self.requests >= previous.requests
            and self.active_duration_seconds >= previous.active_duration_seconds
        )


class RuntimeBudgetLimits(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    max_rounds: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_requests: int | None = Field(default=None, ge=1)
    max_active_duration_seconds: float | None = Field(default=None, gt=0)


class RuntimeCompositionFingerprint(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    composition_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    semantic_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolver_routing_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    routing_policy_id: str = Field(min_length=1, max_length=200)
    priority_provider_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_composition_hash(self) -> RuntimeCompositionFingerprint:
        payload = {
            "semantic_manifest_hash": self.semantic_manifest_hash,
            "resolver_routing_hash": self.resolver_routing_hash,
            "routing_policy_id": self.routing_policy_id,
            "priority_provider_ids": sorted(self.priority_provider_ids),
        }
        expected = "sha256:" + sha256_text(canonical_json(payload))
        if self.composition_hash != expected:
            raise ValueError("runtime composition hash does not match composition fields")
        return self


class TerminationDecision(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    decision_id: str = Field(pattern=r"^termination_[0-9a-f]{16}$")
    kind: TerminationKind
    reason: str = Field(min_length=1, max_length=1_000)
    evaluation_time: datetime

    @model_validator(mode="after")
    def validate_identity(self) -> TerminationDecision:
        if self.evaluation_time.tzinfo is None or self.evaluation_time.utcoffset() is None:
            raise ValueError("termination evaluation_time must be timezone-aware")
        expected = stable_id(
            "termination",
            self.kind.value,
            self.reason,
            self.evaluation_time.isoformat(),
        )
        if self.decision_id != expected:
            raise ValueError("termination decision_id does not match canonical decision")
        return self

    @classmethod
    def create(
        cls,
        *,
        kind: TerminationKind,
        reason: str,
        evaluation_time: datetime,
    ) -> TerminationDecision:
        return cls(
            decision_id=stable_id(
                "termination", kind.value, reason, evaluation_time.isoformat()
            ),
            kind=kind,
            reason=reason,
            evaluation_time=evaluation_time,
        )


class RunFinalization(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    finalization_id: str = Field(pattern=r"^finalization_[0-9a-f]{16}$")
    termination_decision_id: str = Field(pattern=r"^termination_[0-9a-f]{16}$")
    status: FinalizationStatus
    started_at: datetime
    finished_at: datetime
    cleanup_outcome_ids: tuple[str, ...] = Field(default=(), max_length=10_000)

    @model_validator(mode="after")
    def validate_identity(self) -> RunFinalization:
        for value in (self.started_at, self.finished_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("finalization timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finalization cannot finish before it starts")
        if len(set(self.cleanup_outcome_ids)) != len(self.cleanup_outcome_ids):
            raise ValueError("cleanup outcome ids must be unique")
        expected = stable_id(
            "finalization",
            self.termination_decision_id,
            self.status.value,
            self.started_at.isoformat(),
            self.finished_at.isoformat(),
            canonical_json(sorted(self.cleanup_outcome_ids)),
        )
        if self.finalization_id != expected:
            raise ValueError("finalization_id does not match finalization content")
        return self

    @classmethod
    def create(
        cls,
        *,
        termination_decision_id: str,
        status: FinalizationStatus,
        started_at: datetime,
        finished_at: datetime,
        cleanup_outcome_ids: tuple[str, ...] = (),
    ) -> RunFinalization:
        outcomes = tuple(sorted(set(cleanup_outcome_ids)))
        return cls(
            finalization_id=stable_id(
                "finalization",
                termination_decision_id,
                status.value,
                started_at.isoformat(),
                finished_at.isoformat(),
                canonical_json(list(outcomes)),
            ),
            termination_decision_id=termination_decision_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            cleanup_outcome_ids=outcomes,
        )


class RuntimeState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    run_id: str = Field(min_length=1, max_length=200)
    lifecycle: RuntimeLifecycle = RuntimeLifecycle.RUNNING
    usage: RuntimeUsage = Field(default_factory=RuntimeUsage)
    resume_count: int = Field(default=0, ge=0)
    termination: TerminationDecision | None = None
    finalization: RunFinalization | None = None
    last_evaluation_time: datetime | None = None
    recovery_notes: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RuntimeState:
        if self.last_evaluation_time is not None and (
            self.last_evaluation_time.tzinfo is None
            or self.last_evaluation_time.utcoffset() is None
        ):
            raise ValueError("runtime last_evaluation_time must be timezone-aware")
        if self.lifecycle is RuntimeLifecycle.FINALIZED:
            if self.termination is None or self.finalization is None:
                raise ValueError("FINALIZED runtime requires termination and finalization records")
        if self.finalization is not None:
            if self.termination is None:
                raise ValueError("finalization requires a termination decision")
            if self.finalization.termination_decision_id != self.termination.decision_id:
                raise ValueError("finalization termination binding mismatch")
        return self

    @classmethod
    def create(cls, run_id: str) -> RuntimeState:
        return cls(run_id=run_id)
