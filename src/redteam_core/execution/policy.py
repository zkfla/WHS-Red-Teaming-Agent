from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel
from redteam_core.models.execution import (
    CleanupExpectation,
    EffectClass,
    ExecutionPolicySnapshot,
    ExperimentDescriptor,
)
from redteam_core.runtime.clock import RuntimeClock


class ExecutionPolicyViolation(RuntimeError):
    def __init__(self, rule_id: str, detail: str) -> None:
        super().__init__(f"{rule_id}: {detail}")
        self.rule_id = rule_id
        self.detail = detail


class PolicyConstraintDecision(SchemaModel):
    allowed: bool
    reason_code: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=1_000)


@dataclass(frozen=True)
class PolicyConstraintEvaluatorDescriptor:
    evaluator_id: str
    constraint_type_ids: frozenset[str]


class PolicyConstraintEvaluator(Protocol):
    descriptor: PolicyConstraintEvaluatorDescriptor

    def evaluate(
        self,
        descriptor: ExperimentDescriptor,
        constraint: BaseModel,
    ) -> PolicyConstraintDecision: ...


class PolicyConstraintEvaluatorRegistry:
    def __init__(self) -> None:
        self._items: dict[str, PolicyConstraintEvaluator] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, evaluator: PolicyConstraintEvaluator) -> None:
        if self._frozen:
            raise RuntimeError("policy constraint evaluator registry is frozen")
        key = evaluator.descriptor.evaluator_id
        if key in self._items:
            raise ValueError(f"duplicate policy constraint evaluator runtime: {key}")
        self._items[key] = evaluator

    def freeze(self) -> None:
        self._frozen = True

    def require(self, evaluator_id: str) -> PolicyConstraintEvaluator:
        try:
            return self._items[evaluator_id]
        except KeyError as exc:
            raise KeyError(
                f"unregistered policy constraint evaluator runtime: {evaluator_id}"
            ) from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


def _validate_evaluator_manifest(
    catalog: ExtensionCatalog,
    registry: PolicyConstraintEvaluatorRegistry,
) -> None:
    if not catalog.frozen or not registry.frozen:
        raise RuntimeError("policy catalog and evaluator registry must be frozen")
    expected_ids = tuple(
        sorted({item.evaluator_id for item in catalog.policy_constraint_evaluators.values()})
    )
    if registry.ids() != expected_ids:
        raise ValueError("runtime policy evaluator set does not match semantic manifest")
    by_evaluator: dict[str, set[str]] = {}
    for registration in catalog.policy_constraint_evaluators.values():
        by_evaluator.setdefault(registration.evaluator_id, set()).add(
            registration.constraint_type_id
        )
    for evaluator_id, constraint_types in by_evaluator.items():
        runtime = registry.require(evaluator_id)
        if runtime.descriptor.constraint_type_ids != frozenset(constraint_types):
            raise ValueError("runtime policy evaluator applicability differs from manifest")


def compile_execution_policy(
    *,
    catalog: ExtensionCatalog,
    approval_ref: str,
    approved_at,
    allowed_scope_refs: frozenset[str],
    allowed_effect_classes: frozenset[EffectClass],
    allowed_action_type_ids: frozenset[str],
    allowed_capability_ids: frozenset[str] = frozenset(),
    max_attempts: int = 100,
    max_concurrency: int = 1,
    cleanup_required: bool = True,
    stop_condition_ids: tuple[str, ...] = (),
    constraints=(),
) -> ExecutionPolicySnapshot:
    if not catalog.frozen:
        raise RuntimeError("catalog must be frozen before compiling execution policy")
    for action_type_id in allowed_action_type_ids:
        if catalog.experiment_actions.get(action_type_id) is None:
            raise ValueError(f"policy allows unregistered experiment action: {action_type_id}")
    canonical_constraints = tuple(constraints)
    for constraint in canonical_constraints:
        registration = catalog.policy_constraints.require(constraint.type_id)
        registration.decode(constraint)
        if catalog.policy_constraint_evaluators.get(constraint.type_id) is None:
            raise ValueError(f"policy constraint has no evaluator: {constraint.type_id}")
    return ExecutionPolicySnapshot.create(
        approval_ref=approval_ref,
        approved_at=approved_at,
        allowed_scope_refs=allowed_scope_refs,
        allowed_effect_classes=allowed_effect_classes,
        allowed_action_type_ids=allowed_action_type_ids,
        created_from_manifest_hash=catalog.semantic_manifest_hash,
        allowed_capability_ids=allowed_capability_ids,
        max_attempts=max_attempts,
        max_concurrency=max_concurrency,
        cleanup_required=cleanup_required,
        stop_condition_ids=stop_condition_ids,
        constraints=canonical_constraints,
    )


def activate_execution_policy(
    session: CoreSession,
    *,
    policy: ExecutionPolicySnapshot,
    catalog: ExtensionCatalog,
    clock: RuntimeClock,
) -> None:
    if policy.created_from_manifest_hash != catalog.semantic_manifest_hash:
        raise ExecutionPolicyViolation(
            "SEMANTIC_MANIFEST_MISMATCH",
            "policy snapshot was compiled against a different semantic manifest",
        )
    validate_policy_time(policy, clock)
    candidate = session.execution.model_copy(deep=True)
    existing = candidate.policies.get(policy.policy_id)
    if existing is not None and existing != policy:
        raise ValueError("policy ID collision")
    candidate.policies[policy.policy_id] = policy
    if candidate.active_policy_id == policy.policy_id:
        return
    candidate.active_policy_id = policy.policy_id
    candidate.revision += 1
    session.execution = candidate


def _active_concurrency(session: CoreSession) -> int:
    active = 0
    for attempt in session.execution.attempts.values():
        if attempt.status.value != "STARTED":
            continue
        descriptor = session.execution.experiments[attempt.experiment_id]
        active += descriptor.requested_concurrency
    return active


def _started_attempts(session: CoreSession) -> int:
    return sum(item.started_at is not None for item in session.execution.attempts.values())


def enforce_execution_policy(
    session: CoreSession,
    *,
    descriptor: ExperimentDescriptor,
    policy: ExecutionPolicySnapshot,
    catalog: ExtensionCatalog,
    evaluators: PolicyConstraintEvaluatorRegistry,
) -> None:
    _validate_evaluator_manifest(catalog, evaluators)
    if policy.created_from_manifest_hash != catalog.semantic_manifest_hash:
        raise ExecutionPolicyViolation(
            "SEMANTIC_MANIFEST_MISMATCH",
            "policy snapshot was compiled against a different semantic manifest",
        )
    if session.execution.active_policy_id != policy.policy_id:
        raise ExecutionPolicyViolation(
            "POLICY_NOT_ACTIVE",
            "execution requires the currently active immutable policy snapshot",
        )
    if session.execution.stop_reason is not None:
        raise ExecutionPolicyViolation("STOP_CONDITION_ACTIVE", session.execution.stop_reason)
    action_registration = catalog.experiment_actions.require(descriptor.action_type_id)
    action_registration.decode(descriptor.input_payload)
    if descriptor.action_type_id not in policy.allowed_action_type_ids:
        raise ExecutionPolicyViolation(
            "ACTION_NOT_ALLOWED", descriptor.action_type_id
        )
    scope_refs = {descriptor.subject_ref, *descriptor.target_refs}
    missing_scope = sorted(scope_refs - set(policy.allowed_scope_refs))
    if missing_scope:
        raise ExecutionPolicyViolation(
            "OUT_OF_SCOPE_TARGET",
            f"scope refs not approved: {missing_scope}",
        )
    if descriptor.effect_class not in policy.allowed_effect_classes:
        raise ExecutionPolicyViolation(
            "EFFECT_CLASS_FORBIDDEN",
            descriptor.effect_class.value,
        )
    missing_capabilities = sorted(
        set(descriptor.required_capabilities) - set(policy.allowed_capability_ids)
    )
    if missing_capabilities:
        raise ExecutionPolicyViolation(
            "CAPABILITY_FORBIDDEN",
            f"missing explicit capabilities: {missing_capabilities}",
        )
    if descriptor.requested_concurrency > policy.max_concurrency:
        raise ExecutionPolicyViolation(
            "CONCURRENCY_EXCEEDS_CEILING",
            f"{descriptor.requested_concurrency} > {policy.max_concurrency}",
        )
    if _active_concurrency(session) + descriptor.requested_concurrency > policy.max_concurrency:
        raise ExecutionPolicyViolation(
            "ACTIVE_CONCURRENCY_EXCEEDS_CEILING",
            "active plus requested concurrency exceeds policy ceiling",
        )
    if _started_attempts(session) >= policy.max_attempts:
        raise ExecutionPolicyViolation(
            "ATTEMPT_BUDGET_EXHAUSTED",
            f"max attempts reached: {policy.max_attempts}",
        )
    if (
        policy.cleanup_required
        and descriptor.effect_class is not EffectClass.READ_ONLY
        and descriptor.cleanup_expectation is not CleanupExpectation.REQUIRED
    ):
        raise ExecutionPolicyViolation(
            "CLEANUP_REQUIRED",
            "state-changing/high-impact experiment must require cleanup",
        )

    for constraint in policy.constraints:
        registration = catalog.policy_constraints.require(constraint.type_id)
        decoded = registration.decode(constraint)
        evaluator_registration = catalog.policy_constraint_evaluators.require(
            constraint.type_id
        )
        evaluator = evaluators.require(evaluator_registration.evaluator_id)
        decision = evaluator.evaluate(descriptor, decoded)
        if not decision.allowed:
            raise ExecutionPolicyViolation(decision.reason_code, decision.detail)


def request_execution_stop(session: CoreSession, reason: str) -> None:
    reason = reason.strip()
    if not reason:
        raise ValueError("execution stop reason must be non-empty")
    candidate = session.execution.model_copy(deep=True)
    candidate.stop_reason = reason[:1_000]
    candidate.revision += 1
    session.execution = candidate


def validate_policy_time(policy: ExecutionPolicySnapshot, clock: RuntimeClock) -> None:
    now = clock.now()
    if policy.approved_at > now:
        raise ExecutionPolicyViolation(
            "POLICY_APPROVAL_IN_FUTURE",
            "policy approval timestamp is later than evaluation time",
        )
