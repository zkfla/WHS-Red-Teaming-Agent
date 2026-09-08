from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from redteam_core.execution import (
    ExecutionPolicyViolation,
    ExecutorCompletion,
    ExperimentExecutorDescriptor,
    ExperimentExecutorRegistry,
    PolicyConstraintDecision,
    PolicyConstraintEvaluatorDescriptor,
    PolicyConstraintEvaluatorRegistry,
    activate_execution_policy,
    authorize_attempt,
    compile_execution_policy,
    execute_authorized_attempt,
    finalize_unconfigured_cleanup,
    plan_automatic_retry,
    plan_experiment,
    request_execution_stop,
)
from redteam_core.extension import DomainBundle, compose_bundles
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CleanupExpectation,
    CleanupStatus,
    CreatedResourceSpec,
    EffectClass,
    ExperimentAttemptStatus,
    ExperimentDescriptor,
    RetrySemantics,
)
from tests.helpers import FP_B, AlternatePayload, SamplePayload, catalog, reference_bundle

NOW = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
ACTION_TYPE = "reference.experiment.value@1"
RESULT_TYPE = "reference.experiment_result.value@1"
CONSTRAINT_TYPE = "reference.policy.value@1"
EXECUTOR_ID = "reference.executor.value@1"
EVALUATOR_ID = "reference.policy_evaluator.value@1"


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class ReferenceConstraintEvaluator:
    descriptor: PolicyConstraintEvaluatorDescriptor
    calls: list[str]

    def evaluate(self, descriptor, constraint):
        self.calls.append(descriptor.experiment_id)
        assert isinstance(constraint, SamplePayload)
        return PolicyConstraintDecision(
            allowed=constraint.enabled,
            reason_code="REFERENCE_CONSTRAINT",
            detail="constraint disabled" if not constraint.enabled else "allowed",
        )


@dataclass
class ReferenceExecutor:
    descriptor: ExperimentExecutorDescriptor
    completion: ExecutorCompletion
    calls: list[str]
    fail: bool = False

    async def execute(self, descriptor, permit):
        self.calls.append(permit.permit_id)
        if self.fail:
            raise TimeoutError("unknown external completion state")
        return self.completion


def _evaluators():
    calls: list[str] = []
    registry = PolicyConstraintEvaluatorRegistry()
    registry.register(
        ReferenceConstraintEvaluator(
            descriptor=PolicyConstraintEvaluatorDescriptor(
                evaluator_id=EVALUATOR_ID,
                constraint_type_ids=frozenset({CONSTRAINT_TYPE}),
            ),
            calls=calls,
        )
    )
    registry.freeze()
    return registry, calls


def _executors(*, fail: bool = False, resource: bool = False):
    cat = catalog()
    result_payload = cat.experiment_results.require(RESULT_TYPE).envelope_from_model(
        AlternatePayload(count=1)
    )
    resources = (
        (
            CreatedResourceSpec(
                resource_id="resource:created:1",
                resource_type_id="reference.resource.created@1",
            ),
        )
        if resource
        else ()
    )
    calls: list[str] = []
    registry = ExperimentExecutorRegistry()
    registry.register(
        ReferenceExecutor(
            descriptor=ExperimentExecutorDescriptor(
                executor_id=EXECUTOR_ID,
                action_type_ids=frozenset({ACTION_TYPE}),
                result_type_ids=frozenset({RESULT_TYPE}),
            ),
            completion=ExecutorCompletion(
                succeeded=True,
                result_payload=result_payload,
                evidence_ids=("ev_execution",),
                created_resources=resources,
            ),
            calls=calls,
            fail=fail,
        )
    )
    registry.freeze()
    return registry, calls


def _executors_with_invalid_result_and_resource():
    cat = catalog()
    wrong_payload = cat.experiment_actions.require(ACTION_TYPE).envelope_from_model(
        SamplePayload(value="not-a-result")
    )
    calls: list[str] = []
    registry = ExperimentExecutorRegistry()
    registry.register(
        ReferenceExecutor(
            descriptor=ExperimentExecutorDescriptor(
                executor_id=EXECUTOR_ID,
                action_type_ids=frozenset({ACTION_TYPE}),
                result_type_ids=frozenset({RESULT_TYPE}),
            ),
            completion=ExecutorCompletion(
                succeeded=True,
                result_payload=wrong_payload,
                created_resources=(
                    CreatedResourceSpec(
                        resource_id="resource:post-effect:1",
                        resource_type_id="reference.resource.created@1",
                    ),
                ),
            ),
            calls=calls,
        )
    )
    registry.freeze()
    return registry, calls


def _changed_catalog():
    original = reference_bundle()
    changed = DomainBundle(
        manifest=original.manifest.model_copy(update={"implementation_fingerprint": FP_B}),
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    return compose_bundles((changed,))


def _descriptor(
    *,
    effect: EffectClass = EffectClass.READ_ONLY,
    capability: str | None = None,
    concurrency: int = 1,
    retry: RetrySemantics = RetrySemantics.NEVER_AUTOMATIC,
    cleanup: CleanupExpectation = CleanupExpectation.NONE,
) -> ExperimentDescriptor:
    cat = catalog()
    payload = cat.experiment_actions.require(ACTION_TYPE).envelope_from_model(
        SamplePayload(value="execute")
    )
    capabilities = (capability,) if capability else ()
    return ExperimentDescriptor.create(
        action_type_id=ACTION_TYPE,
        subject_ref="subject:one",
        target_refs=("target:one",),
        effect_class=effect,
        input_payload=payload,
        required_capabilities=capabilities,
        requested_concurrency=concurrency,
        retry_semantics=retry,
        cleanup_expectation=cleanup,
        idempotency_key_spec="header:key" if retry is RetrySemantics.IDEMPOTENT else None,
        reconciliation_key_spec=(
            "lookup:semantic-key" if retry is RetrySemantics.RECONCILABLE else None
        ),
    )


def _constraint(*, enabled: bool = True):
    cat = catalog()
    return cat.policy_constraints.require(CONSTRAINT_TYPE).envelope_from_model(
        SamplePayload(value="constraint", enabled=enabled)
    )


def _policy(
    *,
    descriptor: ExperimentDescriptor,
    capabilities: frozenset[str] = frozenset(),
    max_attempts: int = 10,
    max_concurrency: int = 4,
    constraint_enabled: bool = True,
    approved_at: datetime = NOW,
):
    cat = catalog()
    return compile_execution_policy(
        catalog=cat,
        approval_ref="approval:test",
        approved_at=approved_at,
        allowed_scope_refs=frozenset({descriptor.subject_ref, *descriptor.target_refs}),
        allowed_effect_classes=frozenset({descriptor.effect_class}),
        allowed_action_type_ids=frozenset({descriptor.action_type_id}),
        allowed_capability_ids=capabilities,
        max_attempts=max_attempts,
        max_concurrency=max_concurrency,
        cleanup_required=descriptor.effect_class is not EffectClass.READ_ONLY,
        constraints=(_constraint(enabled=constraint_enabled),),
    )


def _activate(session: CoreSession, policy, clock: MutableClock) -> None:
    activate_execution_policy(
        session,
        policy=policy,
        catalog=catalog(),
        clock=clock,
    )


def test_policy_hash_is_deterministic_and_changes_with_effective_scope():
    descriptor = _descriptor()
    first = _policy(descriptor=descriptor)
    second = _policy(descriptor=descriptor)
    assert first.policy_hash == second.policy_hash
    changed = compile_execution_policy(
        catalog=catalog(),
        approval_ref="approval:test",
        approved_at=NOW,
        allowed_scope_refs=frozenset({"subject:one", "target:one", "target:extra"}),
        allowed_effect_classes=frozenset({EffectClass.READ_ONLY}),
        allowed_action_type_ids=frozenset({ACTION_TYPE}),
        constraints=(_constraint(),),
    )
    assert changed.policy_hash != first.policy_hash


def test_high_impact_requires_explicit_capability_in_descriptor_and_policy():
    with pytest.raises(ValidationError, match="explicit capability"):
        _descriptor(effect=EffectClass.HIGH_IMPACT)
    descriptor = _descriptor(
        effect=EffectClass.HIGH_IMPACT,
        capability="reference.race_testing",
        cleanup=CleanupExpectation.REQUIRED,
    )
    session = CoreSession()
    attempt = plan_experiment(
        session, descriptor=descriptor, catalog=catalog(), clock=MutableClock(NOW)
    )
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, MutableClock(NOW))
    with pytest.raises(ExecutionPolicyViolation, match="CAPABILITY_FORBIDDEN"):
        authorize_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            clock=MutableClock(NOW),
        )


def test_constraint_denial_happens_before_authorization():
    descriptor = _descriptor()
    session = CoreSession()
    attempt = plan_experiment(
        session, descriptor=descriptor, catalog=catalog(), clock=MutableClock(NOW)
    )
    evaluators, calls = _evaluators()
    policy = _policy(descriptor=descriptor, constraint_enabled=False)
    _activate(session, policy, MutableClock(NOW))
    with pytest.raises(ExecutionPolicyViolation, match="REFERENCE_CONSTRAINT"):
        authorize_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            clock=MutableClock(NOW),
        )
    assert calls
    assert session.execution.attempts[attempt.attempt_id].status is ExperimentAttemptStatus.PLANNED


@pytest.mark.asyncio
async def test_final_boundary_rechecks_stop_condition_and_never_calls_executor():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    request_execution_stop(session, "operator stop")
    executors, calls = _executors()
    with pytest.raises(ExecutionPolicyViolation, match="STOP_CONDITION_ACTIVE"):
        await execute_authorized_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            executors=executors,
            clock=clock,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_policy_hash_change_cannot_reuse_old_authorization():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    original = _policy(descriptor=descriptor)
    _activate(session, original, clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=original,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    changed = compile_execution_policy(
        catalog=catalog(),
        approval_ref="approval:changed",
        approved_at=NOW,
        allowed_scope_refs=frozenset({"subject:one", "target:one"}),
        allowed_effect_classes=frozenset({EffectClass.READ_ONLY}),
        allowed_action_type_ids=frozenset({ACTION_TYPE}),
        max_attempts=9,
        constraints=(_constraint(),),
    )
    _activate(session, changed, clock)
    executors, calls = _executors()
    with pytest.raises(ExecutionPolicyViolation, match="POLICY_NOT_ACTIVE"):
        await execute_authorized_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=original,
            catalog=catalog(),
            evaluators=evaluators,
            executors=executors,
            clock=clock,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_executor_exception_after_started_is_uncertain_and_retry_unsafe_is_not_replayed():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    executors, calls = _executors(fail=True)
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert calls
    assert result is None
    assert terminal.status is ExperimentAttemptStatus.UNCERTAIN
    with pytest.raises(ValueError, match="not safe"):
        plan_automatic_retry(
            session,
            prior_attempt_id=attempt.attempt_id,
            catalog=catalog(),
            clock=clock,
        )


@pytest.mark.asyncio
async def test_post_effect_result_validation_failure_preserves_cleanup_tracking():
    descriptor = _descriptor(
        effect=EffectClass.STATE_CHANGING,
        cleanup=CleanupExpectation.REQUIRED,
    )
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    executors, calls = _executors_with_invalid_result_and_resource()
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert calls
    assert result is None
    assert terminal.status is ExperimentAttemptStatus.UNCERTAIN
    assert len(session.execution.created_resources) == 1
    assert len(session.execution.cleanup_requirements) == 1


@pytest.mark.asyncio
async def test_idempotent_uncertain_attempt_can_plan_retry_only_with_same_operational_key():
    descriptor = _descriptor(retry=RetrySemantics.IDEMPOTENT)
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    missing_key = plan_experiment(
        session, descriptor=descriptor, catalog=catalog(), clock=clock
    )
    with pytest.raises(ExecutionPolicyViolation, match="IDEMPOTENCY_KEY_REQUIRED"):
        authorize_attempt(
            session,
            attempt_id=missing_key.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            clock=clock,
        )

    attempt = plan_experiment(
        session,
        descriptor=descriptor,
        catalog=catalog(),
        clock=clock,
        idempotency_key="idem:stable",
    )
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    executors, _ = _executors(fail=True)
    terminal, _ = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert terminal.status is ExperimentAttemptStatus.UNCERTAIN
    retry = plan_automatic_retry(
        session,
        prior_attempt_id=attempt.attempt_id,
        catalog=catalog(),
        clock=clock,
    )
    assert retry.idempotency_key == "idem:stable"
    assert retry.retry_of_attempt_id == attempt.attempt_id


@pytest.mark.asyncio
async def test_automatic_retry_cannot_cross_semantic_manifest_change():
    descriptor = _descriptor(retry=RetrySemantics.IDEMPOTENT)
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    attempt = plan_experiment(
        session,
        descriptor=descriptor,
        catalog=catalog(),
        clock=clock,
        idempotency_key="idem:stable",
    )
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    executors, _ = _executors(fail=True)
    terminal, _ = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert terminal.status is ExperimentAttemptStatus.UNCERTAIN
    with pytest.raises(ValueError, match="semantic manifest"):
        plan_automatic_retry(
            session,
            prior_attempt_id=attempt.attempt_id,
            catalog=_changed_catalog(),
            clock=clock,
        )


@pytest.mark.asyncio
async def test_successful_state_change_creates_explicit_cleanup_requirement_and_gap_outcome():
    descriptor = _descriptor(
        effect=EffectClass.STATE_CHANGING,
        cleanup=CleanupExpectation.REQUIRED,
    )
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    executors, _ = _executors(resource=True)
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert terminal.status is ExperimentAttemptStatus.SUCCEEDED
    assert result is not None
    assert len(session.execution.cleanup_requirements) == 1
    outcomes = finalize_unconfigured_cleanup(session, clock=clock)
    assert len(outcomes) == 1
    assert outcomes[0].status is CleanupStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_expired_permit_fails_before_executor_call():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
        permit_ttl_seconds=1,
    )
    clock.value = NOW + timedelta(seconds=2)
    executors, calls = _executors()
    with pytest.raises(ExecutionPolicyViolation, match="PERMIT_EXPIRED"):
        await execute_authorized_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            executors=executors,
            clock=clock,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_final_boundary_rechecks_active_concurrency_after_authorization():
    descriptor = _descriptor(concurrency=1)
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor, max_concurrency=1)
    _activate(session, policy, clock)
    first = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    second = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=first.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    authorize_attempt(
        session,
        attempt_id=second.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    first_authorized = session.execution.attempts[first.attempt_id]
    started = first_authorized.model_copy(
        update={"status": ExperimentAttemptStatus.STARTED, "started_at": clock.now()}
    )
    started = type(first_authorized).model_validate(started.model_dump(mode="python"))
    state = session.execution.model_copy(deep=True)
    state.attempts[first.attempt_id] = started
    state.revision += 1
    session.execution = state

    executors, calls = _executors()
    with pytest.raises(ExecutionPolicyViolation, match="ACTIVE_CONCURRENCY_EXCEEDS_CEILING"):
        await execute_authorized_attempt(
            session,
            attempt_id=second.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            executors=executors,
            clock=clock,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_final_boundary_rechecks_attempt_budget_after_authorization():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor, max_attempts=1, max_concurrency=2)
    _activate(session, policy, clock)
    first = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    second = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=first.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    authorize_attempt(
        session,
        attempt_id=second.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    executors, calls = _executors()
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=first.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert terminal.status is ExperimentAttemptStatus.SUCCEEDED
    assert result is not None
    calls.clear()
    with pytest.raises(ExecutionPolicyViolation, match="ATTEMPT_BUDGET_EXHAUSTED"):
        await execute_authorized_attempt(
            session,
            attempt_id=second.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            executors=executors,
            clock=clock,
        )
    assert calls == []


def test_policy_snapshot_is_immutable():
    descriptor = _descriptor()
    policy = _policy(descriptor=descriptor)
    with pytest.raises(ValidationError):
        policy.max_concurrency = 999


def test_runtime_policy_evaluator_set_must_match_manifest():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    empty_evaluators = PolicyConstraintEvaluatorRegistry()
    empty_evaluators.freeze()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    with pytest.raises(ValueError, match="evaluator set"):
        authorize_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=empty_evaluators,
            clock=clock,
        )



@pytest.mark.asyncio
async def test_runtime_executor_set_must_match_manifest():
    descriptor = _descriptor()
    session = CoreSession()
    clock = MutableClock(NOW)
    evaluators, _ = _evaluators()
    policy = _policy(descriptor=descriptor)
    _activate(session, policy, clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=catalog(), clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=evaluators,
        clock=clock,
    )
    empty_executors = ExperimentExecutorRegistry()
    empty_executors.freeze()
    with pytest.raises(ValueError, match="executor set"):
        await execute_authorized_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=catalog(),
            evaluators=evaluators,
            executors=empty_executors,
            clock=clock,
        )
