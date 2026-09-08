from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id
from redteam_core.models.decision import (
    CompletionAssessmentRecord,
    DecisionAction,
    DecisionPhase,
    DecisionResponseReceipt,
    ProgressState,
)
from redteam_core.models.operational_budget import OperationalBudgetState, ReservationStatus
from redteam_core.models.priority import PriorityState
from redteam_core.models.resolution_batch import ResolutionBatch
from redteam_core.models.resolution_receipt import (
    MAX_RESOLUTION_RECEIPT_LEDGER_BYTES,
    MAX_RESOLUTION_RECEIPTS,
    ResolutionReceipt,
)


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
    decision_driver_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    semantic_pipeline_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    context_policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    progress_policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_composition_hash(self) -> RuntimeCompositionFingerprint:
        payload = {
            "semantic_manifest_hash": self.semantic_manifest_hash,
            "resolver_routing_hash": self.resolver_routing_hash,
            "routing_policy_id": self.routing_policy_id,
            "priority_provider_ids": sorted(self.priority_provider_ids),
            "decision_driver_hash": self.decision_driver_hash,
            "semantic_pipeline_hash": self.semantic_pipeline_hash,
            "context_policy_hash": self.context_policy_hash,
            "progress_policy_hash": self.progress_policy_hash,
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
            decision_id=stable_id("termination", kind.value, reason, evaluation_time.isoformat()),
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
    priority: PriorityState = Field(default_factory=PriorityState)
    progress: ProgressState = Field(default_factory=ProgressState)
    decision_responses: tuple[DecisionResponseReceipt, ...] = Field(
        default=(), max_length=512
    )
    completion_assessments: tuple[CompletionAssessmentRecord, ...] = Field(
        default=(), max_length=256
    )
    resolution_batches: tuple[ResolutionBatch, ...] = Field(default=(), max_length=1_000)
    resolution_receipts: tuple[ResolutionReceipt, ...] = Field(
        default=(), max_length=MAX_RESOLUTION_RECEIPTS
    )
    committed_resolution_hashes: tuple[str, ...] = Field(
        default=(), max_length=MAX_RESOLUTION_RECEIPTS
    )
    operational_accounted_requests: int = Field(default=0, ge=0)
    operational_accounted_tokens: int = Field(default=0, ge=0)
    operational_budget: OperationalBudgetState | None = None
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
        if len({item.step_index for item in self.completion_assessments}) != len(
            self.completion_assessments
        ):
            raise ValueError("duplicate completion assessment step")
        step_by_index = {item.step_index: item for item in self.progress.receipts}
        for record in self.completion_assessments:
            step = step_by_index.get(record.step_index)
            if (
                step is None
                or step.action is not DecisionAction.FINISH
                or step.input_fingerprint != record.input_fingerprint
                or step.before_fingerprint != record.before_fingerprint
            ):
                raise ValueError("completion assessment lacks its finish step binding")
        if len({item.attempt_id for item in self.decision_responses}) != len(
            self.decision_responses
        ):
            raise ValueError("duplicate decision response attempt")
        if sum(item.serialized_bytes for item in self.decision_responses) > 67_108_864:
            raise ValueError("durable decision response ledger exceeds byte budget")
        consumed_decision_attempts = {
            attempt_id
            for receipt in self.progress.receipts
            for attempt_id in receipt.request_attempt_ids
        }
        if sum(len(item.request_attempt_ids) for item in self.progress.receipts) != len(
            consumed_decision_attempts
        ):
            raise ValueError("decision attempt reused across step receipts")
        reservation_by_attempt = {
            item.attempt_id: item
            for item in (
                self.operational_budget.reservations if self.operational_budget else ()
            )
        }
        for attempt_id in consumed_decision_attempts:
            reservation = reservation_by_attempt.get(attempt_id)
            if reservation is None or reservation.status not in {
                ReservationStatus.SUCCEEDED,
                ReservationStatus.FAILED,
            }:
                raise ValueError("step receipt lacks a terminal decision reservation")
        for response in self.decision_responses:
            ledger = self.operational_budget
            reservation = next(
                (
                    row
                    for row in (ledger.reservations if ledger else ())
                    if row.attempt_id == response.attempt_id
                ),
                None,
            )
            if (
                ledger is None
                or reservation is None
                or reservation.status is not ReservationStatus.SUCCEEDED
                or response.run_id != self.run_id
                or response.composition_hash != ledger.composition_hash
                or response.limits_hash != ledger.limits_hash
            ):
                raise ValueError("decision response budget/run binding mismatch")
            if response.attempt_id in consumed_decision_attempts:
                step = next(
                    item
                    for item in self.progress.receipts
                    if response.attempt_id in item.request_attempt_ids
                )
                if response.step_index != step.step_index:
                    raise ValueError("decision response step binding mismatch")
            elif response.step_index != self.progress.next_step_index:
                raise ValueError("pending decision response belongs to another step")
        if self.progress.active_attempt_id is not None:
            reservation = next(
                (
                    row
                    for row in (
                        self.operational_budget.reservations
                        if self.operational_budget
                        else ()
                    )
                    if row.attempt_id == self.progress.active_attempt_id
                ),
                None,
            )
            if reservation is None:
                raise ValueError("active decision attempt lacks operational reservation")
            if self.progress.phase is DecisionPhase.DISPATCH and reservation.status not in {
                ReservationStatus.DISPATCHED,
                ReservationStatus.UNCERTAIN,
            }:
                raise ValueError("active decision dispatch has incompatible reservation state")
        if (
            sum(r.serialized_bytes for r in self.resolution_receipts)
            > MAX_RESOLUTION_RECEIPT_LEDGER_BYTES
        ):
            raise ValueError("durable resolution receipt ledger exceeds byte budget")
        if sum(b.max_serialized_bytes for b in self.resolution_batches) > 16_777_216:
            raise ValueError("durable batch ledger exceeds byte budget")
        batch_ids = set()
        batch_attempts = set()
        receipt_by_hash = {r.receipt_hash: r for r in self.resolution_receipts}
        for batch in self.resolution_batches:
            if batch.batch_id in batch_ids:
                raise ValueError("duplicate resolution batch")
            batch_ids.add(batch.batch_id)
            if self.operational_budget is None or (
                batch.run_id != self.run_id
                or batch.composition_hash != self.operational_budget.composition_hash
            ):
                raise ValueError("resolution batch run/composition mismatch")
            for item in batch.items:
                if set(item.attempt_ids) & batch_attempts:
                    raise ValueError("batch attempt reused")
                batch_attempts.update(item.attempt_ids)
                for receipt_hash in item.receipt_hashes:
                    receipt = receipt_by_hash.get(receipt_hash)
                    if receipt is None or (
                        receipt.attempt_id not in item.attempt_ids
                        or receipt.requirement != item.requirement
                        or receipt.descriptor_hash not in item.descriptor_hashes
                    ):
                        raise ValueError("batch receipt binding mismatch")
                    if (
                        item.fallback_attempt_ids
                        and receipt.descriptor_hash
                        != item.descriptor_hashes[item.attempt_ids.index(receipt.attempt_id)]
                    ):
                        raise ValueError("fallback receipt descriptor order mismatch")
        attempts = set()
        hashes = set()
        for receipt in self.resolution_receipts:
            if receipt.attempt_id in attempts or receipt.receipt_hash in hashes:
                raise ValueError("duplicate resolution receipt")
            attempts.add(receipt.attempt_id)
            hashes.add(receipt.receipt_hash)
            ledger = self.operational_budget
            if ledger is None or receipt.run_id != self.run_id:
                raise ValueError("resolution receipt requires its run budget")
            if (receipt.composition_hash, receipt.limits_hash) != (
                ledger.composition_hash,
                ledger.limits_hash,
            ):
                raise ValueError("resolution receipt budget binding mismatch")
            reservation = next(
                (r for r in ledger.reservations if r.attempt_id == receipt.attempt_id), None
            )
            result = receipt.observation or receipt.failure
            if reservation is None or reservation.resolver_id != result.resolver_id:
                raise ValueError("resolution receipt dispatch binding mismatch")
            if reservation.provider_id != receipt.requirement.provider_id:
                raise ValueError("resolution receipt provider binding mismatch")
            allowed = (
                {ReservationStatus.SUCCEEDED}
                if receipt.observation is not None
                else {ReservationStatus.FAILED, ReservationStatus.UNCERTAIN}
            )
            if reservation.status not in allowed:
                raise ValueError("resolution receipt status mismatch")
        if len(set(self.committed_resolution_hashes)) != len(self.committed_resolution_hashes):
            raise ValueError("duplicate committed resolution hash")
        if set(self.committed_resolution_hashes) - hashes:
            raise ValueError("committed resolution hash has no receipt")
        if self.operational_budget is None:
            if self.operational_accounted_requests or self.operational_accounted_tokens:
                raise ValueError("operational accounting requires its ledger")
        elif (
            self.operational_accounted_requests > self.operational_budget.dispatched_requests
            or self.operational_accounted_tokens > self.operational_budget.measured_tokens
        ):
            raise ValueError("operational accounted usage exceeds its ledger")
        if (
            self.usage.requests < self.operational_accounted_requests
            or self.usage.tokens < self.operational_accounted_tokens
        ):
            raise ValueError("runtime usage is below its operational accounted floor")
        if self.operational_budget is not None and self.operational_budget.run_id != self.run_id:
            raise ValueError("operational budget run binding mismatch")
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
