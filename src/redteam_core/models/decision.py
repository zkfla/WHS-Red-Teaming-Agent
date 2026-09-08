"""Typed, provider-neutral decision and progress contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id
from redteam_core.models.reasoning import WorkingMemoryPatch


class DecisionAction(StrEnum):
    QUERY_CONTEXT = "QUERY_CONTEXT"
    RESOLVE_REQUIREMENT = "RESOLVE_REQUIREMENT"
    PATCH_WORKING_MEMORY = "PATCH_WORKING_MEMORY"
    REQUEST_EXPERIMENT_PLAN = "REQUEST_EXPERIMENT_PLAN"
    FINISH = "FINISH"


class DecisionProposal(SchemaModel):
    """One narrow action. Extra executor/validator/evidence fields are forbidden."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    action: DecisionAction
    expected_knowledge_revision: int = Field(ge=0)
    expected_candidate_revision: int = Field(ge=0)
    expected_working_memory_revision: int = Field(ge=0)
    expected_expectation_revision: int = Field(ge=0)
    candidate_id: str | None = Field(default=None, pattern=r"^cand_[0-9a-f]{16}$")
    requirement_id: str | None = Field(default=None, pattern=r"^req_[0-9a-f]{16}$")
    retrieval_ref: str | None = Field(default=None, min_length=1, max_length=500)
    memory_patch: WorkingMemoryPatch | None = None
    finish_reason: str | None = Field(default=None, min_length=1, max_length=500)

    def _identity_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json", exclude={"proposal_id"})
        return payload

    @model_validator(mode="after")
    def validate_action_shape(self) -> DecisionProposal:
        present = {
            "candidate_id": self.candidate_id is not None,
            "requirement_id": self.requirement_id is not None,
            "retrieval_ref": self.retrieval_ref is not None,
            "memory_patch": self.memory_patch is not None,
            "finish_reason": self.finish_reason is not None,
        }
        allowed = {
            DecisionAction.QUERY_CONTEXT: {"candidate_id", "retrieval_ref"},
            DecisionAction.RESOLVE_REQUIREMENT: {"requirement_id"},
            DecisionAction.PATCH_WORKING_MEMORY: {"memory_patch"},
            DecisionAction.REQUEST_EXPERIMENT_PLAN: {"candidate_id"},
            DecisionAction.FINISH: {"finish_reason"},
        }[self.action]
        if {key for key, value in present.items() if value} != allowed:
            raise ValueError("proposal arguments do not match its action")
        expected = stable_id("proposal", canonical_json(self._identity_payload()))
        if self.proposal_id != expected:
            raise ValueError("proposal_id does not match canonical proposal")
        return self

    @classmethod
    def create(cls, *, action: DecisionAction, **kwargs) -> DecisionProposal:
        payload = {"action": action, **kwargs}
        provisional = cls.model_construct(proposal_id="proposal_0000000000000000", **payload)
        return cls(
            proposal_id=stable_id("proposal", canonical_json(provisional._identity_payload())),
            **payload,
        )


class DecisionDriverDescriptor(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    driver_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=200)
    tokenizer_id: str = Field(min_length=1, max_length=200)
    max_input_tokens: int = Field(ge=1, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    structured_output_schema_tokens: int = Field(default=0, ge=0, le=100_000)
    hard_output_token_bound: bool = True
    max_response_bytes: int = Field(default=65_536, ge=4_096, le=1_048_576)

    @property
    def descriptor_hash(self) -> str:
        return "sha256:" + sha256_text(canonical_json(self.model_dump(mode="json")))


class DecisionDriverReply(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    payload: dict[str, object]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class DecisionResponseReceipt(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    receipt_id: str = Field(pattern=r"^decision_response_[0-9a-f]{16}$")
    run_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    step_index: int = Field(ge=1)
    ordinal: int = Field(ge=0, le=1)
    composition_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    limits_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    driver_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    control_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    before_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reply: DecisionDriverReply
    serialized_bytes: int = Field(ge=1, le=1_048_576)

    def _payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"receipt_id", "serialized_bytes"})

    @model_validator(mode="after")
    def validate_receipt(self) -> DecisionResponseReceipt:
        payload = canonical_json(self._payload())
        if self.receipt_id != stable_id("decision_response", payload):
            raise ValueError("decision response receipt identity mismatch")
        expected_bytes = len(payload.encode("utf-8"))
        if self.serialized_bytes != expected_bytes:
            raise ValueError("decision response receipt byte count mismatch")
        return self

    @classmethod
    def create(cls, **kwargs) -> DecisionResponseReceipt:
        provisional = cls.model_construct(
            receipt_id="decision_response_0000000000000000",
            serialized_bytes=1,
            **kwargs,
        )
        payload = canonical_json(provisional._payload())
        return cls(
            receipt_id=stable_id("decision_response", payload),
            serialized_bytes=len(payload.encode("utf-8")),
            **kwargs,
        )


class DecisionPhase(StrEnum):
    IDLE = "IDLE"
    SNAPSHOT = "SNAPSHOT"
    PROPOSE = "PROPOSE"
    VALIDATE_PROPOSAL = "VALIDATE_PROPOSAL"
    RESERVE = "RESERVE"
    DISPATCH = "DISPATCH"
    RECORD_COMMIT = "RECORD_COMMIT"
    RECONCILE = "RECONCILE"
    ASSESS = "ASSESS"


class DecisionStepOutcome(StrEnum):
    PROGRESSED = "PROGRESSED"
    NO_PROGRESS = "NO_PROGRESS"
    WAITING = "WAITING"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    STOP_PARTIAL = "STOP_PARTIAL"
    FINISH_REQUESTED = "FINISH_REQUESTED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class ProgressPolicy(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    policy_id: str = "core.progress.semantic-window@1"
    recent_window: int = Field(default=8, ge=4, le=64)
    identical_noop_threshold: int = Field(default=3, ge=2, le=16)
    waiting_poll_limit: int = Field(default=3, ge=1, le=16)
    max_replans: int = Field(default=1, ge=0, le=8)
    malformed_repair_limit: int = Field(default=1, ge=0, le=1)
    budget_drop_percent: int = Field(default=50, ge=1, le=100)


class ReplanReason(StrEnum):
    NEW_EVIDENCE = "NEW_EVIDENCE"
    DEPENDENCY_CONFLICT = "DEPENDENCY_CONFLICT"
    BLOCKED_ROOT = "BLOCKED_ROOT"
    BUDGET_DROP = "BUDGET_DROP"


class ReplanEvaluation(SchemaModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_index: int = Field(ge=1)
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    conflict_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    blocked_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    remaining_requests: int = Field(ge=0)
    remaining_tokens: int | None = Field(default=None, ge=0)
    reasons: tuple[ReplanReason, ...] = Field(default=(), max_length=4)


class DecisionStepReceipt(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    receipt_id: str = Field(pattern=r"^step_[0-9a-f]{16}$")
    step_index: int = Field(ge=1)
    proposal_id: str | None = Field(default=None, pattern=r"^proposal_[0-9a-f]{16}$")
    action: DecisionAction | None = None
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalized_outcome: str = Field(min_length=1, max_length=500)
    outcome: DecisionStepOutcome
    changed: bool = False
    request_attempt_ids: tuple[str, ...] = Field(default=(), max_length=2)
    before_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    after_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> DecisionStepReceipt:
        if self.changed != (self.before_fingerprint != self.after_fingerprint):
            raise ValueError("step changed flag disagrees with semantic fingerprints")
        payload = self.model_dump(mode="json", exclude={"receipt_id"})
        if self.receipt_id != stable_id("step", canonical_json(payload)):
            raise ValueError("step receipt identity mismatch")
        return self

    @classmethod
    def create(cls, **kwargs) -> DecisionStepReceipt:
        provisional = cls.model_construct(receipt_id="step_0000000000000000", **kwargs)
        payload = provisional.model_dump(mode="json", exclude={"receipt_id"})
        return cls(receipt_id=stable_id("step", canonical_json(payload)), **kwargs)


class ContextRetrievalReceipt(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    receipt_id: str = Field(pattern=r"^context_retrieval_[0-9a-f]{16}$")
    step_index: int = Field(ge=1)
    retrieval_ref: str = Field(min_length=1, max_length=500)
    complete: bool
    content: str = Field(min_length=2, max_length=48_000)
    utf8_bytes: int = Field(ge=2, le=192_000)

    @model_validator(mode="after")
    def validate_receipt(self) -> ContextRetrievalReceipt:
        if self.utf8_bytes != len(self.content.encode("utf-8")):
            raise ValueError("context retrieval byte count mismatch")
        payload = self.model_dump(mode="json", exclude={"receipt_id"})
        if self.receipt_id != stable_id("context_retrieval", canonical_json(payload)):
            raise ValueError("context retrieval identity mismatch")
        return self

    @classmethod
    def create(cls, **kwargs) -> ContextRetrievalReceipt:
        provisional = cls.model_construct(receipt_id="context_retrieval_0000000000000000", **kwargs)
        payload = provisional.model_dump(mode="json", exclude={"receipt_id"})
        return cls(receipt_id=stable_id("context_retrieval", canonical_json(payload)), **kwargs)


class ProgressState(SchemaModel):
    policy: ProgressPolicy = Field(default_factory=ProgressPolicy)
    phase: DecisionPhase = DecisionPhase.IDLE
    next_step_index: int = Field(default=1, ge=1)
    active_attempt_id: str | None = Field(default=None, max_length=200)
    replan_count: int = Field(default=0, ge=0)
    receipts: tuple[DecisionStepReceipt, ...] = Field(default=(), max_length=256)
    replan_evaluations: tuple[ReplanEvaluation, ...] = Field(default=(), max_length=256)
    context_retrievals: tuple[ContextRetrievalReceipt, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_progress(self) -> ProgressState:
        if self.replan_count > self.policy.max_replans:
            raise ValueError("replan count exceeds policy")
        indices = [row.step_index for row in self.replan_evaluations]
        if indices != sorted(set(indices)) or any(i > self.next_step_index for i in indices):
            raise ValueError("replan evaluation indices must be unique ordered past/current steps")
        if self.active_attempt_id is None and self.phase is DecisionPhase.DISPATCH:
            raise ValueError("dispatch phase requires active attempt")
        if len({item.step_index for item in self.receipts}) != len(self.receipts):
            raise ValueError("duplicate durable step index")
        if self.receipts and max(item.step_index for item in self.receipts) >= self.next_step_index:
            raise ValueError("next step index must follow all receipts")
        if len({item.step_index for item in self.context_retrievals}) != len(
            self.context_retrievals
        ):
            raise ValueError("duplicate context retrieval step")
        receipt_steps = {item.step_index for item in self.receipts}
        if any(item.step_index not in receipt_steps for item in self.context_retrievals):
            raise ValueError("context retrieval lacks its decision step receipt")
        return self


class CompletionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class ScopeCompletion(SchemaModel):
    acquisition_complete: bool = False
    search_complete: bool = False
    search_truncated: bool = False


class CompletionAssessment(SchemaModel):
    stale_decision_root_keys: tuple[str, ...] = Field(default=(), max_length=4096)
    status: CompletionStatus
    reasons: tuple[str, ...]
    candidate_count: int = Field(ge=0)
    unresolved_requirement_ids: tuple[str, ...] = ()
    blocked_root_keys: tuple[str, ...] = ()
    uncertain_effect_ids: tuple[str, ...] = ()
    pending_experiment_ids: tuple[str, ...] = ()
    cleanup_gap_ids: tuple[str, ...] = ()


class CompletionAssessmentRecord(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    record_id: str = Field(pattern=r"^completion_[0-9a-f]{16}$")
    step_index: int = Field(ge=1)
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    before_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    assessment: CompletionAssessment

    @model_validator(mode="after")
    def validate_record(self) -> CompletionAssessmentRecord:
        payload = self.model_dump(mode="json", exclude={"record_id"})
        if self.record_id != stable_id("completion", canonical_json(payload)):
            raise ValueError("completion assessment record identity mismatch")
        return self

    @classmethod
    def create(cls, **kwargs) -> CompletionAssessmentRecord:
        provisional = cls.model_construct(record_id="completion_0000000000000000", **kwargs)
        payload = provisional.model_dump(mode="json", exclude={"record_id"})
        return cls(record_id=stable_id("completion", canonical_json(payload)), **kwargs)
