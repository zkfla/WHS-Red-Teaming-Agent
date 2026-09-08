from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from pydantic import Field

from redteam_core.execution.policy import (
    ExecutionPolicyViolation,
    PolicyConstraintEvaluatorRegistry,
    enforce_execution_policy,
    validate_policy_time,
)
from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel, canonical_json, stable_id
from redteam_core.models.execution import (
    CleanupExpectation,
    CleanupRequirement,
    CreatedResource,
    CreatedResourceSpec,
    ExecutionPermit,
    ExecutionPolicySnapshot,
    ExperimentAttempt,
    ExperimentAttemptStatus,
    ExperimentDescriptor,
    ExperimentResult,
    RetrySemantics,
)
from redteam_core.models.provenance import TypedPayloadEnvelope
from redteam_core.runtime.clock import RuntimeClock


class ExecutorCompletion(SchemaModel):
    succeeded: bool
    result_payload: TypedPayloadEnvelope | None = None
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=256)
    created_resources: tuple[CreatedResourceSpec, ...] = Field(default=(), max_length=256)
    failure_reason_code: str | None = Field(default=None, max_length=300)


@dataclass(frozen=True)
class ExperimentExecutorDescriptor:
    executor_id: str
    action_type_ids: frozenset[str]
    result_type_ids: frozenset[str] = frozenset()


class ExperimentExecutor(Protocol):
    descriptor: ExperimentExecutorDescriptor

    async def execute(
        self,
        descriptor: ExperimentDescriptor,
        permit: ExecutionPermit,
    ) -> ExecutorCompletion: ...


class ExperimentEffectObserver(Protocol):
    def effect_started(
        self,
        *,
        attempt: ExperimentAttempt,
        descriptor: ExperimentDescriptor,
    ) -> None: ...


class EffectObserverTerminalError(RuntimeError):
    pass

    def effect_terminal(
        self,
        *,
        attempt: ExperimentAttempt,
        result: ExperimentResult | None,
    ) -> None: ...


class ExperimentExecutorRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ExperimentExecutor] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, executor: ExperimentExecutor) -> None:
        if self._frozen:
            raise RuntimeError("experiment executor registry is frozen")
        key = executor.descriptor.executor_id
        if key in self._items:
            raise ValueError(f"duplicate experiment executor runtime: {key}")
        self._items[key] = executor

    def freeze(self) -> None:
        self._frozen = True

    def require(self, executor_id: str) -> ExperimentExecutor:
        try:
            return self._items[executor_id]
        except KeyError as exc:
            raise KeyError(f"unregistered experiment executor runtime: {executor_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


def _validate_executor_manifest(
    catalog: ExtensionCatalog,
    registry: ExperimentExecutorRegistry,
) -> None:
    if not catalog.frozen or not registry.frozen:
        raise RuntimeError("experiment catalog and executor registry must be frozen")
    expected = tuple(item.executor_id for item in catalog.experiment_executors.values())
    if registry.ids() != expected:
        raise ValueError("runtime experiment executor set does not match semantic manifest")
    for registration in catalog.experiment_executors.values():
        runtime = registry.require(registration.executor_id)
        if runtime.descriptor.action_type_ids != registration.action_type_ids:
            raise ValueError("runtime executor action types differ from semantic manifest")
        if runtime.descriptor.result_type_ids != registration.result_type_ids:
            raise ValueError("runtime executor result types differ from semantic manifest")


def _executor_registration_for_action(catalog: ExtensionCatalog, action_type_id: str):
    matches = [
        item
        for item in catalog.experiment_executors.values()
        if action_type_id in item.action_type_ids
    ]
    if len(matches) != 1:
        raise ValueError(
            f"experiment action must resolve to exactly one executor: {action_type_id}"
        )
    return matches[0]


def plan_experiment(
    session: CoreSession,
    *,
    descriptor: ExperimentDescriptor,
    catalog: ExtensionCatalog,
    clock: RuntimeClock,
    idempotency_key: str | None = None,
    reconciliation_key: str | None = None,
    retry_of_attempt_id: str | None = None,
) -> ExperimentAttempt:
    if not catalog.frozen:
        raise RuntimeError("catalog must be frozen before planning an experiment")
    validated = ExperimentDescriptor.model_validate(descriptor.model_dump(mode="python"))
    action = catalog.experiment_actions.require(validated.action_type_id)
    action.decode(validated.input_payload)
    _executor_registration_for_action(catalog, validated.action_type_id)

    candidate = session.execution.model_copy(deep=True)
    existing = candidate.experiments.get(validated.experiment_id)
    if existing is not None and existing != validated:
        raise ValueError("experiment ID collision with different descriptor")
    candidate.experiments[validated.experiment_id] = validated
    sequence = candidate.attempt_sequence + 1
    attempt = ExperimentAttempt(
        attempt_id=stable_id("attempt", validated.experiment_id, sequence),
        sequence=sequence,
        experiment_id=validated.experiment_id,
        experiment_descriptor_hash=validated.descriptor_hash,
        semantic_manifest_hash=catalog.semantic_manifest_hash,
        status=ExperimentAttemptStatus.PLANNED,
        idempotency_key=idempotency_key,
        reconciliation_key=reconciliation_key,
        retry_of_attempt_id=retry_of_attempt_id,
        planned_at=clock.now(),
    )
    candidate.attempt_sequence = sequence
    candidate.attempts[attempt.attempt_id] = attempt
    candidate.revision += 1
    session.execution = candidate
    return attempt


def _retry_binding_is_safe(
    descriptor: ExperimentDescriptor,
    attempt: ExperimentAttempt,
) -> bool:
    if descriptor.retry_semantics is RetrySemantics.NEVER_AUTOMATIC:
        return False
    if descriptor.retry_semantics is RetrySemantics.IDEMPOTENT:
        return bool(attempt.idempotency_key)
    if descriptor.retry_semantics is RetrySemantics.RECONCILABLE:
        return bool(attempt.reconciliation_key)
    return False


def plan_automatic_retry(
    session: CoreSession,
    *,
    prior_attempt_id: str,
    catalog: ExtensionCatalog,
    clock: RuntimeClock,
) -> ExperimentAttempt:
    prior = session.execution.attempts.get(prior_attempt_id)
    if prior is None:
        raise ValueError("unknown prior attempt")
    if prior.status not in {ExperimentAttemptStatus.FAILED, ExperimentAttemptStatus.UNCERTAIN}:
        raise ValueError("automatic retry requires FAILED or UNCERTAIN prior attempt")
    if prior.semantic_manifest_hash != catalog.semantic_manifest_hash:
        raise ValueError("automatic retry cannot cross semantic manifest changes")
    descriptor = session.execution.experiments[prior.experiment_id]
    if not _retry_binding_is_safe(descriptor, prior):
        raise ValueError("automatic retry is not safe for this experiment")
    return plan_experiment(
        session,
        descriptor=descriptor,
        catalog=catalog,
        clock=clock,
        idempotency_key=prior.idempotency_key,
        reconciliation_key=prior.reconciliation_key,
        retry_of_attempt_id=prior.attempt_id,
    )


def authorize_attempt(
    session: CoreSession,
    *,
    attempt_id: str,
    policy: ExecutionPolicySnapshot,
    catalog: ExtensionCatalog,
    evaluators: PolicyConstraintEvaluatorRegistry,
    clock: RuntimeClock,
    permit_ttl_seconds: int = 300,
) -> ExecutionPermit:
    if permit_ttl_seconds <= 0 or permit_ttl_seconds > 86_400:
        raise ValueError("permit_ttl_seconds must be within 1..86400")
    attempt = session.execution.attempts.get(attempt_id)
    if attempt is None:
        raise ValueError("unknown experiment attempt")
    if attempt.status is not ExperimentAttemptStatus.PLANNED:
        raise ValueError("only PLANNED attempt can be authorized")
    descriptor = session.execution.experiments[attempt.experiment_id]
    if attempt.experiment_descriptor_hash != descriptor.descriptor_hash:
        raise ValueError("attempt descriptor binding is stale")
    if attempt.semantic_manifest_hash != catalog.semantic_manifest_hash:
        raise ValueError("attempt semantic manifest binding is stale")
    if session.execution.active_policy_id != policy.policy_id:
        raise ExecutionPolicyViolation(
            "POLICY_NOT_ACTIVE",
            "attempt authorization requires the active execution policy",
        )
    validate_policy_time(policy, clock)
    enforce_execution_policy(
        session,
        descriptor=descriptor,
        policy=policy,
        catalog=catalog,
        evaluators=evaluators,
    )
    if descriptor.retry_semantics is RetrySemantics.IDEMPOTENT and not attempt.idempotency_key:
        raise ExecutionPolicyViolation(
            "IDEMPOTENCY_KEY_REQUIRED",
            "IDEMPOTENT experiment requires an operational idempotency key",
        )
    if descriptor.retry_semantics is RetrySemantics.RECONCILABLE and not attempt.reconciliation_key:
        raise ExecutionPolicyViolation(
            "RECONCILIATION_KEY_REQUIRED",
            "RECONCILABLE experiment requires an operational reconciliation key",
        )

    issued_at = clock.now()
    expires_at = issued_at + timedelta(seconds=permit_ttl_seconds)
    permit = ExecutionPermit(
        permit_id=stable_id(
            "permit",
            attempt.attempt_id,
            descriptor.descriptor_hash,
            policy.policy_hash,
            catalog.semantic_manifest_hash,
            issued_at.isoformat(),
            expires_at.isoformat(),
        ),
        attempt_id=attempt.attempt_id,
        experiment_id=descriptor.experiment_id,
        experiment_descriptor_hash=descriptor.descriptor_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        semantic_manifest_hash=catalog.semantic_manifest_hash,
        allowed_effect_class=descriptor.effect_class,
        capability_refs=tuple(sorted(descriptor.required_capabilities)),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    authorized = attempt.model_copy(
        update={
            "status": ExperimentAttemptStatus.AUTHORIZED,
            "policy_hash": policy.policy_hash,
            "permit_id": permit.permit_id,
            "authorized_at": issued_at,
        }
    )
    authorized = ExperimentAttempt.model_validate(authorized.model_dump(mode="python"))
    candidate = session.execution.model_copy(deep=True)
    candidate.permits[permit.permit_id] = permit
    candidate.attempts[attempt.attempt_id] = authorized
    candidate.revision += 1
    session.execution = candidate
    return permit


def _validate_result_payload(
    *,
    completion: ExecutorCompletion,
    executor_registration,
    catalog: ExtensionCatalog,
):
    payload = completion.result_payload
    if payload is None:
        return None
    if not hasattr(payload, "type_id"):
        raise ValueError("executor result payload must be a TypedPayloadEnvelope")
    if payload.type_id not in executor_registration.result_type_ids:
        raise ValueError("executor emitted undeclared result payload type")
    registration = catalog.experiment_results.require(payload.type_id)
    registration.decode(payload)
    return payload


async def execute_authorized_attempt(
    session: CoreSession,
    *,
    attempt_id: str,
    policy: ExecutionPolicySnapshot,
    catalog: ExtensionCatalog,
    evaluators: PolicyConstraintEvaluatorRegistry,
    executors: ExperimentExecutorRegistry,
    clock: RuntimeClock,
    effect_observer: ExperimentEffectObserver | None = None,
) -> tuple[ExperimentAttempt, ExperimentResult | None]:
    _validate_executor_manifest(catalog, executors)
    attempt = session.execution.attempts.get(attempt_id)
    if attempt is None:
        raise ValueError("unknown experiment attempt")
    if attempt.status is not ExperimentAttemptStatus.AUTHORIZED:
        raise ValueError("attempt must be AUTHORIZED before external effect")
    if attempt.permit_id is None:
        raise ValueError("authorized attempt has no permit")
    permit = session.execution.permits[attempt.permit_id]
    descriptor = session.execution.experiments[attempt.experiment_id]

    now = clock.now()
    if now > permit.expires_at:
        raise ExecutionPolicyViolation("PERMIT_EXPIRED", permit.permit_id)
    if permit.experiment_descriptor_hash != descriptor.descriptor_hash:
        raise ExecutionPolicyViolation("DESCRIPTOR_BINDING_MISMATCH", descriptor.experiment_id)
    if permit.semantic_manifest_hash != catalog.semantic_manifest_hash:
        raise ExecutionPolicyViolation("SEMANTIC_MANIFEST_MISMATCH", permit.permit_id)
    if permit.policy_hash != policy.policy_hash or permit.policy_id != policy.policy_id:
        raise ExecutionPolicyViolation(
            "POLICY_BINDING_MISMATCH",
            "current policy does not match the authorized immutable snapshot",
        )
    if permit.allowed_effect_class is not descriptor.effect_class:
        raise ExecutionPolicyViolation("EFFECT_BINDING_MISMATCH", descriptor.effect_class.value)
    if set(permit.capability_refs) != set(descriptor.required_capabilities):
        raise ExecutionPolicyViolation("CAPABILITY_BINDING_MISMATCH", permit.permit_id)
    enforce_execution_policy(
        session,
        descriptor=descriptor,
        policy=policy,
        catalog=catalog,
        evaluators=evaluators,
    )

    started_at = clock.now()
    started = attempt.model_copy(
        update={"status": ExperimentAttemptStatus.STARTED, "started_at": started_at}
    )
    started = ExperimentAttempt.model_validate(started.model_dump(mode="python"))
    candidate = session.execution.model_copy(deep=True)
    candidate.attempts[attempt.attempt_id] = started
    candidate.revision += 1
    session.execution = candidate

    if effect_observer is not None:
        try:
            effect_observer.effect_started(attempt=started, descriptor=descriptor)
        except Exception as exc:
            completed_at = clock.now()
            failed = started.model_copy(
                update={
                    "status": ExperimentAttemptStatus.FAILED,
                    "completed_at": completed_at,
                    "failure_reason_code": f"EFFECT_START_OBSERVER:{type(exc).__name__}",
                }
            )
            failed = ExperimentAttempt.model_validate(failed.model_dump(mode="python"))
            candidate = session.execution.model_copy(deep=True)
            candidate.attempts[attempt.attempt_id] = failed
            candidate.revision += 1
            session.execution = candidate
            raise RuntimeError(
                "effect-start observer failed; external effect was not invoked"
            ) from exc

    executor_registration = _executor_registration_for_action(catalog, descriptor.action_type_id)
    executor = executors.require(executor_registration.executor_id)
    reported_resources: tuple[CreatedResource, ...] = ()
    try:
        completion = await executor.execute(descriptor, permit)
        completion = ExecutorCompletion.model_validate(completion.model_dump(mode="python"))
        reported_resources = tuple(
            CreatedResource(
                created_resource_id=stable_id(
                    "created",
                    item.resource_type_id,
                    item.resource_id,
                    attempt.attempt_id,
                ),
                resource_id=item.resource_id,
                resource_type_id=item.resource_type_id,
                created_by_attempt_id=attempt.attempt_id,
                subject_ref=item.subject_ref,
                cleanup_required=(
                    item.cleanup_required
                    or policy.cleanup_required
                    or descriptor.cleanup_expectation is CleanupExpectation.REQUIRED
                ),
            )
            for item in completion.created_resources
        )
        payload = _validate_result_payload(
            completion=completion,
            executor_registration=executor_registration,
            catalog=catalog,
        )
        completed_at = clock.now()
        payload_hash = payload.content_hash if payload is not None else ""
        result = ExperimentResult(
            result_id=stable_id(
                "result",
                attempt.attempt_id,
                completion.succeeded,
                payload_hash,
                canonical_json(sorted(completion.evidence_ids)),
            ),
            experiment_id=descriptor.experiment_id,
            attempt_id=attempt.attempt_id,
            succeeded=completion.succeeded,
            result_payload=payload,
            evidence_ids=tuple(sorted(set(completion.evidence_ids))),
            created_resources=reported_resources,
            completed_at=completed_at,
        )
        terminal_status = (
            ExperimentAttemptStatus.SUCCEEDED
            if completion.succeeded
            else ExperimentAttemptStatus.FAILED
        )
        terminal = started.model_copy(
            update={
                "status": terminal_status,
                "completed_at": completed_at,
                "result_id": result.result_id,
                "failure_reason_code": (
                    None
                    if completion.succeeded
                    else completion.failure_reason_code or "EXECUTOR_FAILED"
                ),
            }
        )
        terminal = ExperimentAttempt.model_validate(terminal.model_dump(mode="python"))
        candidate = session.execution.model_copy(deep=True)
        candidate.results[result.result_id] = result
        candidate.attempts[attempt.attempt_id] = terminal
        for resource in reported_resources:
            existing_resource = candidate.created_resources.get(resource.created_resource_id)
            if existing_resource is not None and existing_resource != resource:
                raise ValueError("created resource ID collision")
            candidate.created_resources[resource.created_resource_id] = resource
            if resource.cleanup_required:
                requirement = CleanupRequirement.for_resource(resource)
                candidate.cleanup_requirements[requirement.cleanup_requirement_id] = requirement
        candidate.revision += 1
        session.execution = candidate
        if effect_observer is not None:
            try:
                effect_observer.effect_terminal(attempt=terminal, result=result)
            except Exception as exc:
                raise EffectObserverTerminalError(
                    "effect-terminal observer failed after execution state was committed"
                ) from exc
        return terminal, result
    except EffectObserverTerminalError:
        raise
    except Exception as exc:
        completed_at = clock.now()
        uncertain = started.model_copy(
            update={
                "status": ExperimentAttemptStatus.UNCERTAIN,
                "completed_at": completed_at,
                "failure_reason_code": f"{type(exc).__name__}",
            }
        )
        uncertain = ExperimentAttempt.model_validate(uncertain.model_dump(mode="python"))
        candidate = session.execution.model_copy(deep=True)
        candidate.attempts[attempt.attempt_id] = uncertain
        for resource in reported_resources:
            existing_resource = candidate.created_resources.get(resource.created_resource_id)
            if existing_resource is not None and existing_resource != resource:
                continue
            candidate.created_resources[resource.created_resource_id] = resource
            if resource.cleanup_required:
                requirement = CleanupRequirement.for_resource(resource)
                candidate.cleanup_requirements[requirement.cleanup_requirement_id] = requirement
        candidate.revision += 1
        session.execution = candidate
        if effect_observer is not None:
            effect_observer.effect_terminal(attempt=uncertain, result=None)
        return uncertain, None
