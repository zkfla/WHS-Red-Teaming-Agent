from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id
from redteam_core.models.provenance import TYPE_ID_PATTERN, TypedPayloadEnvelope


class EffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    STATE_CHANGING = "STATE_CHANGING"
    HIGH_IMPACT = "HIGH_IMPACT"


class RetrySemantics(StrEnum):
    NEVER_AUTOMATIC = "NEVER_AUTOMATIC"
    IDEMPOTENT = "IDEMPOTENT"
    RECONCILABLE = "RECONCILABLE"


class CleanupExpectation(StrEnum):
    NONE = "NONE"
    BEST_EFFORT = "BEST_EFFORT"
    REQUIRED = "REQUIRED"


class ExperimentAttemptStatus(StrEnum):
    PLANNED = "PLANNED"
    AUTHORIZED = "AUTHORIZED"
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"

    @property
    def terminal(self) -> bool:
        return self in {
            ExperimentAttemptStatus.SUCCEEDED,
            ExperimentAttemptStatus.FAILED,
            ExperimentAttemptStatus.UNCERTAIN,
        }


class CleanupStatus(StrEnum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    NOT_CONFIGURED = "NOT_CONFIGURED"

    @property
    def terminal(self) -> bool:
        return self in {
            CleanupStatus.SUCCESS,
            CleanupStatus.FAILED,
            CleanupStatus.UNCERTAIN,
            CleanupStatus.NOT_CONFIGURED,
        }


class CleanupAttemptStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    NOT_CONFIGURED = "NOT_CONFIGURED"

    @property
    def terminal(self) -> bool:
        return self is not CleanupAttemptStatus.STARTED


class ExperimentDescriptor(SchemaModel):
    experiment_id: str = Field(pattern=r"^experiment_[0-9a-f]{16}$")
    action_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    subject_ref: str = Field(min_length=1, max_length=500)
    target_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    required_capabilities: tuple[str, ...] = Field(default=(), max_length=64)
    effect_class: EffectClass
    requested_concurrency: int = Field(default=1, ge=1, le=10_000)
    retry_semantics: RetrySemantics = RetrySemantics.NEVER_AUTOMATIC
    cleanup_expectation: CleanupExpectation = CleanupExpectation.NONE
    input_payload: TypedPayloadEnvelope
    idempotency_key_spec: str | None = Field(default=None, max_length=300)
    reconciliation_key_spec: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_descriptor(self) -> ExperimentDescriptor:
        if self.input_payload.type_id != self.action_type_id:
            raise ValueError("experiment input payload type must equal action_type_id")
        if len(set(self.target_refs)) != len(self.target_refs):
            raise ValueError("experiment target refs must be unique")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("experiment required capabilities must be unique")
        if self.retry_semantics is RetrySemantics.IDEMPOTENT and not self.idempotency_key_spec:
            raise ValueError("IDEMPOTENT retry semantics require idempotency_key_spec")
        if self.retry_semantics is RetrySemantics.RECONCILABLE and not self.reconciliation_key_spec:
            raise ValueError("RECONCILABLE retry semantics require reconciliation_key_spec")
        if self.effect_class is EffectClass.HIGH_IMPACT and not self.required_capabilities:
            raise ValueError("HIGH_IMPACT experiment requires an explicit capability")
        expected = self._expected_id()
        if self.experiment_id != expected:
            raise ValueError("experiment_id does not match canonical descriptor identity")
        return self

    def _identity_payload(self) -> dict[str, object]:
        return {
            "action_type_id": self.action_type_id,
            "subject_ref": self.subject_ref,
            "target_refs": sorted(self.target_refs),
            "required_capabilities": sorted(self.required_capabilities),
            "effect_class": self.effect_class.value,
            "requested_concurrency": self.requested_concurrency,
            "retry_semantics": self.retry_semantics.value,
            "cleanup_expectation": self.cleanup_expectation.value,
            "input_payload_hash": self.input_payload.content_hash,
            "idempotency_key_spec": self.idempotency_key_spec,
            "reconciliation_key_spec": self.reconciliation_key_spec,
        }

    def _expected_id(self) -> str:
        return stable_id("experiment", canonical_json(self._identity_payload()))

    @property
    def descriptor_hash(self) -> str:
        return "sha256:" + sha256_text(canonical_json(self._identity_payload()))

    @classmethod
    def create(
        cls,
        *,
        action_type_id: str,
        subject_ref: str,
        target_refs: tuple[str, ...],
        effect_class: EffectClass,
        input_payload: TypedPayloadEnvelope,
        required_capabilities: tuple[str, ...] = (),
        requested_concurrency: int = 1,
        retry_semantics: RetrySemantics = RetrySemantics.NEVER_AUTOMATIC,
        cleanup_expectation: CleanupExpectation = CleanupExpectation.NONE,
        idempotency_key_spec: str | None = None,
        reconciliation_key_spec: str | None = None,
    ) -> ExperimentDescriptor:
        payload = {
            "action_type_id": action_type_id,
            "subject_ref": subject_ref,
            "target_refs": sorted(target_refs),
            "required_capabilities": sorted(required_capabilities),
            "effect_class": effect_class.value,
            "requested_concurrency": requested_concurrency,
            "retry_semantics": retry_semantics.value,
            "cleanup_expectation": cleanup_expectation.value,
            "input_payload_hash": input_payload.content_hash,
            "idempotency_key_spec": idempotency_key_spec,
            "reconciliation_key_spec": reconciliation_key_spec,
        }
        return cls(
            experiment_id=stable_id("experiment", canonical_json(payload)),
            action_type_id=action_type_id,
            subject_ref=subject_ref,
            target_refs=tuple(sorted(target_refs)),
            required_capabilities=tuple(sorted(required_capabilities)),
            effect_class=effect_class,
            requested_concurrency=requested_concurrency,
            retry_semantics=retry_semantics,
            cleanup_expectation=cleanup_expectation,
            input_payload=input_payload,
            idempotency_key_spec=idempotency_key_spec,
            reconciliation_key_spec=reconciliation_key_spec,
        )


class ExecutionPolicySnapshot(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    policy_id: str = Field(pattern=r"^policy_[0-9a-f]{16}$")
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_ref: str = Field(min_length=1, max_length=500)
    approved_at: datetime
    allowed_scope_refs: frozenset[str] = Field(min_length=1, max_length=2_000)
    allowed_effect_classes: frozenset[EffectClass] = Field(min_length=1)
    allowed_capability_ids: frozenset[str] = Field(default_factory=frozenset, max_length=1_000)
    allowed_action_type_ids: frozenset[str] = Field(min_length=1, max_length=1_000)
    max_attempts: int = Field(default=100, ge=1, le=1_000_000)
    max_concurrency: int = Field(default=1, ge=1, le=100_000)
    cleanup_required: bool = True
    stop_condition_ids: tuple[str, ...] = Field(default=(), max_length=128)
    constraints: tuple[TypedPayloadEnvelope, ...] = Field(default=(), max_length=128)
    effective_constraints_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_from_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_policy(self) -> ExecutionPolicySnapshot:
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("policy approved_at must be timezone-aware")
        if len(set(self.stop_condition_ids)) != len(self.stop_condition_ids):
            raise ValueError("stop condition ids must be unique")
        expected_constraints = self._constraints_hash(self.constraints)
        if self.effective_constraints_hash != expected_constraints:
            raise ValueError("effective_constraints_hash does not match typed constraints")
        expected_hash = self._effective_policy_hash()
        if self.policy_hash != expected_hash:
            raise ValueError("policy_hash does not match effective policy fields")
        expected_id = stable_id(
            "policy",
            self.policy_hash,
            self.approval_ref,
            self.approved_at.isoformat(),
        )
        if self.policy_id != expected_id:
            raise ValueError("policy_id does not match policy approval identity")
        return self

    @staticmethod
    def _constraints_hash(constraints: tuple[TypedPayloadEnvelope, ...]) -> str:
        rows = sorted(
            (item.type_id, item.schema_version, item.content_hash) for item in constraints
        )
        return "sha256:" + sha256_text(canonical_json(rows))

    def _effective_payload(self) -> dict[str, object]:
        return {
            "allowed_scope_refs": sorted(self.allowed_scope_refs),
            "allowed_effect_classes": sorted(item.value for item in self.allowed_effect_classes),
            "allowed_capability_ids": sorted(self.allowed_capability_ids),
            "allowed_action_type_ids": sorted(self.allowed_action_type_ids),
            "max_attempts": self.max_attempts,
            "max_concurrency": self.max_concurrency,
            "cleanup_required": self.cleanup_required,
            "stop_condition_ids": sorted(self.stop_condition_ids),
            "effective_constraints_hash": self.effective_constraints_hash,
            "created_from_manifest_hash": self.created_from_manifest_hash,
        }

    def _effective_policy_hash(self) -> str:
        return "sha256:" + sha256_text(canonical_json(self._effective_payload()))

    @classmethod
    def create(
        cls,
        *,
        approval_ref: str,
        approved_at: datetime,
        allowed_scope_refs: frozenset[str],
        allowed_effect_classes: frozenset[EffectClass],
        allowed_action_type_ids: frozenset[str],
        created_from_manifest_hash: str,
        allowed_capability_ids: frozenset[str] = frozenset(),
        max_attempts: int = 100,
        max_concurrency: int = 1,
        cleanup_required: bool = True,
        stop_condition_ids: tuple[str, ...] = (),
        constraints: tuple[TypedPayloadEnvelope, ...] = (),
    ) -> ExecutionPolicySnapshot:
        constraints_hash = cls._constraints_hash(constraints)
        effective_payload = {
            "allowed_scope_refs": sorted(allowed_scope_refs),
            "allowed_effect_classes": sorted(item.value for item in allowed_effect_classes),
            "allowed_capability_ids": sorted(allowed_capability_ids),
            "allowed_action_type_ids": sorted(allowed_action_type_ids),
            "max_attempts": max_attempts,
            "max_concurrency": max_concurrency,
            "cleanup_required": cleanup_required,
            "stop_condition_ids": sorted(stop_condition_ids),
            "effective_constraints_hash": constraints_hash,
            "created_from_manifest_hash": created_from_manifest_hash,
        }
        policy_hash = "sha256:" + sha256_text(canonical_json(effective_payload))
        return cls(
            policy_id=stable_id("policy", policy_hash, approval_ref, approved_at.isoformat()),
            policy_hash=policy_hash,
            approval_ref=approval_ref,
            approved_at=approved_at,
            allowed_scope_refs=allowed_scope_refs,
            allowed_effect_classes=allowed_effect_classes,
            allowed_capability_ids=allowed_capability_ids,
            allowed_action_type_ids=allowed_action_type_ids,
            max_attempts=max_attempts,
            max_concurrency=max_concurrency,
            cleanup_required=cleanup_required,
            stop_condition_ids=tuple(sorted(stop_condition_ids)),
            constraints=constraints,
            effective_constraints_hash=constraints_hash,
            created_from_manifest_hash=created_from_manifest_hash,
        )


class ExecutionPermit(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    permit_id: str = Field(pattern=r"^permit_[0-9a-f]{16}$")
    attempt_id: str = Field(pattern=r"^attempt_[0-9a-f]{16}$")
    experiment_id: str = Field(pattern=r"^experiment_[0-9a-f]{16}$")
    experiment_descriptor_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_id: str = Field(pattern=r"^policy_[0-9a-f]{16}$")
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    semantic_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    allowed_effect_class: EffectClass
    capability_refs: tuple[str, ...] = Field(default=(), max_length=64)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_permit(self) -> ExecutionPermit:
        for value, name in ((self.issued_at, "issued_at"), (self.expires_at, "expires_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"permit {name} must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("permit must expire after it is issued")
        if len(set(self.capability_refs)) != len(self.capability_refs):
            raise ValueError("permit capability refs must be unique")
        expected = stable_id(
            "permit",
            self.attempt_id,
            self.experiment_descriptor_hash,
            self.policy_hash,
            self.semantic_manifest_hash,
            self.issued_at.isoformat(),
            self.expires_at.isoformat(),
        )
        if self.permit_id != expected:
            raise ValueError("permit_id does not match permit bindings")
        return self


class ExperimentAttempt(SchemaModel):
    attempt_id: str = Field(pattern=r"^attempt_[0-9a-f]{16}$")
    sequence: int = Field(ge=1)
    experiment_id: str = Field(pattern=r"^experiment_[0-9a-f]{16}$")
    experiment_descriptor_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    semantic_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: ExperimentAttemptStatus = ExperimentAttemptStatus.PLANNED
    policy_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    permit_id: str | None = Field(default=None, pattern=r"^permit_[0-9a-f]{16}$")
    idempotency_key: str | None = Field(default=None, max_length=500)
    reconciliation_key: str | None = Field(default=None, max_length=500)
    retry_of_attempt_id: str | None = Field(default=None, pattern=r"^attempt_[0-9a-f]{16}$")
    planned_at: datetime
    authorized_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_id: str | None = Field(default=None, pattern=r"^result_[0-9a-f]{16}$")
    failure_reason_code: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_attempt(self) -> ExperimentAttempt:
        timestamps = (self.planned_at, self.authorized_at, self.started_at, self.completed_at)
        for value in timestamps:
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("attempt timestamps must be timezone-aware")
        expected = stable_id("attempt", self.experiment_id, self.sequence)
        if self.attempt_id != expected:
            raise ValueError("attempt_id does not match experiment sequence")
        if self.status is ExperimentAttemptStatus.PLANNED:
            if self.policy_hash or self.permit_id or self.authorized_at:
                raise ValueError("PLANNED attempt cannot carry authorization bindings")
        else:
            if not self.policy_hash or not self.permit_id or self.authorized_at is None:
                raise ValueError("authorized-or-later attempt requires policy and permit bindings")
        if self.status in {
            ExperimentAttemptStatus.STARTED,
            ExperimentAttemptStatus.SUCCEEDED,
            ExperimentAttemptStatus.FAILED,
            ExperimentAttemptStatus.UNCERTAIN,
        } and self.started_at is None:
            raise ValueError("started-or-later attempt requires started_at")
        if self.status.terminal and self.completed_at is None:
            raise ValueError("terminal attempt requires completed_at")
        if self.status is ExperimentAttemptStatus.SUCCEEDED and self.result_id is None:
            raise ValueError("SUCCEEDED attempt requires result_id")
        return self


class CreatedResource(SchemaModel):
    created_resource_id: str = Field(pattern=r"^created_[0-9a-f]{16}$")
    resource_id: str = Field(min_length=1, max_length=500)
    resource_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    created_by_attempt_id: str = Field(pattern=r"^attempt_[0-9a-f]{16}$")
    subject_ref: str | None = Field(default=None, max_length=500)
    cleanup_required: bool = True

    @model_validator(mode="after")
    def validate_identity(self) -> CreatedResource:
        expected = stable_id(
            "created",
            self.resource_type_id,
            self.resource_id,
            self.created_by_attempt_id,
        )
        if self.created_resource_id != expected:
            raise ValueError("created_resource_id does not match resource identity")
        return self


class CreatedResourceSpec(SchemaModel):
    resource_id: str = Field(min_length=1, max_length=500)
    resource_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    subject_ref: str | None = Field(default=None, max_length=500)
    cleanup_required: bool = True


class ExperimentResult(SchemaModel):
    result_id: str = Field(pattern=r"^result_[0-9a-f]{16}$")
    experiment_id: str = Field(pattern=r"^experiment_[0-9a-f]{16}$")
    attempt_id: str = Field(pattern=r"^attempt_[0-9a-f]{16}$")
    succeeded: bool
    result_payload: TypedPayloadEnvelope | None = None
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=256)
    created_resources: tuple[CreatedResource, ...] = Field(default=(), max_length=256)
    completed_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> ExperimentResult:
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("experiment result completed_at must be timezone-aware")
        payload_hash = self.result_payload.content_hash if self.result_payload else ""
        expected = stable_id(
            "result",
            self.attempt_id,
            self.succeeded,
            payload_hash,
            canonical_json(sorted(self.evidence_ids)),
        )
        if self.result_id != expected:
            raise ValueError("result_id does not match execution result content")
        if any(item.created_by_attempt_id != self.attempt_id for item in self.created_resources):
            raise ValueError("created resource must bind to the producing attempt")
        return self


class CleanupRequirement(SchemaModel):
    cleanup_requirement_id: str = Field(pattern=r"^cleanup_req_[0-9a-f]{16}$")
    created_resource_id: str = Field(pattern=r"^created_[0-9a-f]{16}$")
    resource_id: str = Field(min_length=1, max_length=500)
    resource_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    created_by_attempt_id: str = Field(pattern=r"^attempt_[0-9a-f]{16}$")

    @classmethod
    def for_resource(cls, resource: CreatedResource) -> CleanupRequirement:
        return cls(
            cleanup_requirement_id=stable_id(
                "cleanup_req",
                resource.created_resource_id,
            ),
            created_resource_id=resource.created_resource_id,
            resource_id=resource.resource_id,
            resource_type_id=resource.resource_type_id,
            created_by_attempt_id=resource.created_by_attempt_id,
        )


class CleanupAttempt(SchemaModel):
    cleanup_attempt_id: str = Field(pattern=r"^cleanup_attempt_[0-9a-f]{16}$")
    sequence: int = Field(ge=1)
    cleanup_requirement_id: str = Field(pattern=r"^cleanup_req_[0-9a-f]{16}$")
    status: CleanupAttemptStatus
    started_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> CleanupAttempt:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("cleanup started_at must be timezone-aware")
        if self.completed_at is not None and (
            self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None
        ):
            raise ValueError("cleanup completed_at must be timezone-aware")
        if self.status.terminal and self.completed_at is None:
            raise ValueError("terminal cleanup attempt requires completed_at")
        if not self.status.terminal and self.completed_at is not None:
            raise ValueError("started cleanup attempt cannot already be completed")
        expected = stable_id("cleanup_attempt", self.cleanup_requirement_id, self.sequence)
        if self.cleanup_attempt_id != expected:
            raise ValueError("cleanup_attempt_id does not match cleanup sequence")
        return self


class CleanupOutcome(SchemaModel):
    cleanup_outcome_id: str = Field(pattern=r"^cleanup_out_[0-9a-f]{16}$")
    cleanup_requirement_id: str = Field(pattern=r"^cleanup_req_[0-9a-f]{16}$")
    cleanup_attempt_id: str = Field(pattern=r"^cleanup_attempt_[0-9a-f]{16}$")
    status: CleanupStatus
    detail: str = Field(default="", max_length=1_000)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=128)
    completed_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> CleanupOutcome:
        if not self.status.terminal:
            raise ValueError("cleanup outcome must be terminal")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("cleanup outcome completed_at must be timezone-aware")
        expected = stable_id(
            "cleanup_out",
            self.cleanup_requirement_id,
            self.cleanup_attempt_id,
            self.status.value,
            self.detail,
            canonical_json(sorted(self.evidence_ids)),
        )
        if self.cleanup_outcome_id != expected:
            raise ValueError("cleanup_outcome_id does not match cleanup result")
        return self


class ExecutionState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    attempt_sequence: int = Field(default=0, ge=0)
    cleanup_attempt_sequence: int = Field(default=0, ge=0)
    experiments: dict[str, ExperimentDescriptor] = Field(default_factory=dict)
    policies: dict[str, ExecutionPolicySnapshot] = Field(default_factory=dict)
    active_policy_id: str | None = Field(default=None, pattern=r"^policy_[0-9a-f]{16}$")
    permits: dict[str, ExecutionPermit] = Field(default_factory=dict)
    attempts: dict[str, ExperimentAttempt] = Field(default_factory=dict)
    results: dict[str, ExperimentResult] = Field(default_factory=dict)
    created_resources: dict[str, CreatedResource] = Field(default_factory=dict)
    cleanup_requirements: dict[str, CleanupRequirement] = Field(default_factory=dict)
    cleanup_attempts: dict[str, CleanupAttempt] = Field(default_factory=dict)
    cleanup_outcomes: dict[str, CleanupOutcome] = Field(default_factory=dict)
    stop_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_ledgers(self) -> ExecutionState:
        if any(key != item.experiment_id for key, item in self.experiments.items()):
            raise ValueError("experiment ledger key mismatch")
        if any(key != item.policy_id for key, item in self.policies.items()):
            raise ValueError("policy ledger key mismatch")
        if self.active_policy_id is not None and self.active_policy_id not in self.policies:
            raise ValueError("active policy references unknown policy snapshot")
        if any(key != item.permit_id for key, item in self.permits.items()):
            raise ValueError("permit ledger key mismatch")
        if any(key != item.attempt_id for key, item in self.attempts.items()):
            raise ValueError("attempt ledger key mismatch")
        if any(key != item.result_id for key, item in self.results.items()):
            raise ValueError("result ledger key mismatch")
        if any(key != item.created_resource_id for key, item in self.created_resources.items()):
            raise ValueError("created resource ledger key mismatch")
        if any(
            key != item.cleanup_requirement_id
            for key, item in self.cleanup_requirements.items()
        ):
            raise ValueError("cleanup requirement ledger key mismatch")
        if any(key != item.cleanup_attempt_id for key, item in self.cleanup_attempts.items()):
            raise ValueError("cleanup attempt ledger key mismatch")
        if any(key != item.cleanup_outcome_id for key, item in self.cleanup_outcomes.items()):
            raise ValueError("cleanup outcome ledger key mismatch")
        for attempt in self.attempts.values():
            descriptor = self.experiments.get(attempt.experiment_id)
            if descriptor is None:
                raise ValueError("attempt references unknown experiment")
            if descriptor.descriptor_hash != attempt.experiment_descriptor_hash:
                raise ValueError("attempt descriptor hash does not match canonical experiment")
            if attempt.policy_hash is not None and not any(
                policy.policy_hash == attempt.policy_hash for policy in self.policies.values()
            ):
                raise ValueError("attempt references unknown policy hash")
            if attempt.permit_id is not None and attempt.permit_id not in self.permits:
                raise ValueError("attempt references unknown permit")
            if attempt.result_id is not None and attempt.result_id not in self.results:
                raise ValueError("attempt references unknown result")
            if attempt.retry_of_attempt_id is not None:
                prior = self.attempts.get(attempt.retry_of_attempt_id)
                if prior is None:
                    raise ValueError("retry attempt references unknown prior attempt")
                if prior.experiment_id != attempt.experiment_id:
                    raise ValueError("retry attempt must reference the same experiment")
        for result in self.results.values():
            attempt = self.attempts.get(result.attempt_id)
            if attempt is None:
                raise ValueError("result references unknown attempt")
            if result.experiment_id != attempt.experiment_id:
                raise ValueError("result experiment does not match attempt")
        for resource in self.created_resources.values():
            if resource.created_by_attempt_id not in self.attempts:
                raise ValueError("created resource references unknown attempt")
        for requirement in self.cleanup_requirements.values():
            resource = self.created_resources.get(requirement.created_resource_id)
            if resource is None:
                raise ValueError("cleanup requirement references unknown resource")
            if resource.resource_id != requirement.resource_id:
                raise ValueError("cleanup requirement external resource id mismatch")
            if resource.created_by_attempt_id != requirement.created_by_attempt_id:
                raise ValueError("cleanup requirement attempt binding mismatch")
        for attempt in self.cleanup_attempts.values():
            if attempt.cleanup_requirement_id not in self.cleanup_requirements:
                raise ValueError("cleanup attempt references unknown cleanup requirement")
        for outcome in self.cleanup_outcomes.values():
            if outcome.cleanup_requirement_id not in self.cleanup_requirements:
                raise ValueError("cleanup outcome references unknown requirement")
            attempt = self.cleanup_attempts.get(outcome.cleanup_attempt_id)
            if attempt is None:
                raise ValueError("cleanup outcome references unknown cleanup attempt")
            if attempt.cleanup_requirement_id != outcome.cleanup_requirement_id:
                raise ValueError("cleanup outcome requirement does not match cleanup attempt")
        return self
