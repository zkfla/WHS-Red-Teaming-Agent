from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from redteam_core.execution import (
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
    plan_experiment,
)
from redteam_core.investigation import reconcile_candidate_state
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    ClaimContract,
    ClaimStatus,
    EffectClass,
    ExperimentDescriptor,
    RootCauseRef,
    SecurityCandidate,
    ValidationVerdict,
)
from redteam_core.runtime import FixedClock
from redteam_core.validation import (
    ValidationDecision,
    ValidatorDescriptor,
    ValidatorRuntimeRegistry,
    run_validation,
)
from tests.helpers import AlternatePayload, SamplePayload, catalog

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
ACTION_TYPE = "reference.experiment.value@1"
RESULT_TYPE = "reference.experiment_result.value@1"
CONSTRAINT_TYPE = "reference.policy.value@1"
EXECUTOR_ID = "reference.executor.value@1"
EVALUATOR_ID = "reference.policy_evaluator.value@1"
VALIDATOR_ID = "reference.validator.value@1"
CANDIDATE_TYPE = "reference.candidate.value@1"


@dataclass
class AllowConstraint:
    descriptor: PolicyConstraintEvaluatorDescriptor

    def evaluate(self, descriptor, constraint):
        assert isinstance(constraint, SamplePayload)
        return PolicyConstraintDecision(allowed=True, reason_code="REFERENCE_ALLOW")


@dataclass
class ReferenceExecutor:
    descriptor: ExperimentExecutorDescriptor
    completion: ExecutorCompletion

    async def execute(self, descriptor, permit):
        return self.completion


@dataclass
class ResultValidator:
    descriptor: ValidatorDescriptor

    def evaluate(self, context):
        assert context.experiment_result is not None
        return (
            ValidationDecision(
                verdict=ValidationVerdict.VIOLATION,
                invariant_id="INV-REFERENCE-RUNTIME",
                reason="runtime result violated the reference invariant",
                evidence_ids=context.experiment_result.evidence_ids,
            ),
        )


@pytest.mark.asyncio
async def test_batch_d_candidate_experiment_policy_result_validation_finding_spine():
    cat = catalog()
    session = CoreSession()
    candidate = SecurityCandidate.create(
        candidate_type_id=CANDIDATE_TYPE,
        semantic_key="batchd:runtime:one",
        root_cause=RootCauseRef(
            owner_bundle_id="reference",
            family_id="runtime",
            key="one",
        ),
        claim_contract=ClaimContract.create(
            claim_type_id=CANDIDATE_TYPE,
            title="Reference runtime claim",
            max_claim_status=ClaimStatus.SUPPORTED,
        ),
        max_claim_status=ClaimStatus.SUPPORTED,
        current_claim_status=ClaimStatus.CANDIDATE,
    )
    reconcile_candidate_state(session, (candidate,))

    action = cat.experiment_actions.require(ACTION_TYPE).envelope_from_model(
        SamplePayload(value="execute")
    )
    descriptor = ExperimentDescriptor.create(
        action_type_id=ACTION_TYPE,
        subject_ref="subject:one",
        target_refs=("target:one",),
        effect_class=EffectClass.READ_ONLY,
        input_payload=action,
    )
    constraint = cat.policy_constraints.require(CONSTRAINT_TYPE).envelope_from_model(
        SamplePayload(value="allow", enabled=True)
    )
    policy = compile_execution_policy(
        catalog=cat,
        approval_ref="approval:batch-d",
        approved_at=NOW,
        allowed_scope_refs=frozenset({"subject:one", "target:one"}),
        allowed_effect_classes=frozenset({EffectClass.READ_ONLY}),
        allowed_action_type_ids=frozenset({ACTION_TYPE}),
        constraints=(constraint,),
    )
    clock = FixedClock(NOW)
    activate_execution_policy(session, policy=policy, catalog=cat, clock=clock)

    evaluators = PolicyConstraintEvaluatorRegistry()
    evaluators.register(
        AllowConstraint(
            descriptor=PolicyConstraintEvaluatorDescriptor(
                evaluator_id=EVALUATOR_ID,
                constraint_type_ids=frozenset({CONSTRAINT_TYPE}),
            )
        )
    )
    evaluators.freeze()

    result_payload = cat.experiment_results.require(RESULT_TYPE).envelope_from_model(
        AlternatePayload(count=1)
    )
    executors = ExperimentExecutorRegistry()
    executors.register(
        ReferenceExecutor(
            descriptor=ExperimentExecutorDescriptor(
                executor_id=EXECUTOR_ID,
                action_type_ids=frozenset({ACTION_TYPE}),
                result_type_ids=frozenset({RESULT_TYPE}),
            ),
            completion=ExecutorCompletion(
                succeeded=True,
                result_payload=result_payload,
                evidence_ids=("ev_batchd_runtime",),
            ),
        )
    )
    executors.freeze()

    attempt = plan_experiment(session, descriptor=descriptor, catalog=cat, clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=cat,
        evaluators=evaluators,
        clock=clock,
    )
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=cat,
        evaluators=evaluators,
        executors=executors,
        clock=clock,
    )
    assert terminal.status.value == "SUCCEEDED"
    assert result is not None

    validators = ValidatorRuntimeRegistry()
    validators.register(
        ResultValidator(descriptor=ValidatorDescriptor(validator_id=VALIDATOR_ID))
    )
    validators.freeze()
    run = run_validation(
        session,
        candidate=session.candidate_state.candidates[candidate.candidate_id],
        catalog=cat,
        registry=validators,
        experiment_result=result,
    )

    assert len(run.outcome_ids) == 1
    outcome = session.validation.outcomes[run.outcome_ids[0]]
    assert outcome.verdict is ValidationVerdict.VIOLATION
    assert outcome.experiment_result_id == result.result_id
    assert len(run.finding_ids) == 1
    finding = session.validation.findings[run.finding_ids[0]]
    assert finding.source_outcome_ids == [outcome.outcome_id]
    assert "ev_batchd_runtime" in finding.evidence_ids
