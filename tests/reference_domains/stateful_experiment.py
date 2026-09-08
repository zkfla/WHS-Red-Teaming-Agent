from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field

from redteam_core.execution import (
    ExecutorCompletion,
    ExperimentExecutorDescriptor,
    PolicyConstraintDecision,
    PolicyConstraintEvaluatorDescriptor,
)
from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    CandidateDeriverRegistration,
    DomainBundle,
    ExperimentExecutorRegistration,
    PolicyConstraintEvaluatorRegistration,
    SchemaCategory,
    SchemaRegistration,
    ValidatorRegistration,
)
from redteam_core.investigation import CandidateDeriverDescriptor
from redteam_core.models.base import SchemaModel
from redteam_core.models.security import (
    ClaimContract,
    ClaimStatus,
    RootCauseRef,
    SecurityCandidate,
    ValidationVerdict,
)
from redteam_core.validation import ValidationDecision, ValidatorDescriptor
from tests.reference_domains.common import fp

BUNDLE_ID = "refstate"
EXPECTATION_TYPE = "refstate.expectation.counter@1"
CANDIDATE_TYPE = "refstate.candidate.delta@1"
ACTION_TYPE = "refstate.experiment.mutate@1"
RESULT_TYPE = "refstate.result.delta@1"
CONSTRAINT_TYPE = "refstate.policy.delta_limit@1"
DERIVER_ID = "refstate.deriver.delta@1"
VALIDATOR_ID = "refstate.validator.invariant@1"
EXECUTOR_ID = "refstate.executor.counter@1"
EVALUATOR_ID = "refstate.policy_evaluator.delta@1"


class BaselinePayload(SchemaModel):
    expected_before: int
    expected_after: int


class CandidatePayload(SchemaModel):
    subject_ref: str = Field(min_length=1, max_length=200)


class MutationAction(SchemaModel):
    delta: int = Field(ge=-1000, le=1000)


class DeltaResult(SchemaModel):
    before: int
    after: int


class DeltaConstraint(SchemaModel):
    max_abs_delta: int = Field(ge=0, le=1000)


def bundle() -> DomainBundle:
    schemas = (
        (SchemaCategory.EXPECTATION, EXPECTATION_TYPE, BaselinePayload, "a"),
        (SchemaCategory.CANDIDATE, CANDIDATE_TYPE, CandidatePayload, "b"),
        (SchemaCategory.EXPERIMENT_ACTION, ACTION_TYPE, MutationAction, "c"),
        (SchemaCategory.EXPERIMENT_RESULT, RESULT_TYPE, DeltaResult, "d"),
        (SchemaCategory.POLICY_CONSTRAINT, CONSTRAINT_TYPE, DeltaConstraint, "e"),
    )
    return DomainBundle(
        manifest=BundleManifest(
            bundle_id=BUNDLE_ID,
            namespace=BUNDLE_ID,
            version="1.0.0",
            bundle_kind=BundleKind.REFERENCE,
            implementation_fingerprint=fp("9"),
        ),
        schema_registrations=tuple(
            SchemaRegistration(
                category=category,
                type_id=type_id,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                model=model,
                implementation_fingerprint=fp(marker),
            )
            for category, type_id, model, marker in schemas
        ),
        candidate_deriver_registrations=(
            CandidateDeriverRegistration(
                deriver_id=DERIVER_ID,
                owner_bundle_id=BUNDLE_ID,
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                implementation_fingerprint=fp("f"),
            ),
        ),
        validator_registrations=(
            ValidatorRegistration(
                validator_id=VALIDATOR_ID,
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("0"),
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                requires_experiment_result=True,
            ),
        ),
        experiment_executor_registrations=(
            ExperimentExecutorRegistration(
                executor_id=EXECUTOR_ID,
                owner_bundle_id=BUNDLE_ID,
                action_type_ids=frozenset({ACTION_TYPE}),
                result_type_ids=frozenset({RESULT_TYPE}),
                implementation_fingerprint=fp("1"),
            ),
        ),
        policy_constraint_evaluator_registrations=(
            PolicyConstraintEvaluatorRegistration(
                constraint_type_id=CONSTRAINT_TYPE,
                evaluator_id=EVALUATOR_ID,
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("2"),
            ),
        ),
    )


@dataclass(frozen=True)
class PathlessDeltaDeriver:
    descriptor: CandidateDeriverDescriptor = field(
        default_factory=lambda: CandidateDeriverDescriptor(deriver_id=DERIVER_ID)
    )

    def derive(self, session, catalog):
        expectations = tuple(
            item
            for item in session.expectations.expectations.values()
            if item.expectation_type_id == EXPECTATION_TYPE
        )
        authoritative = tuple(item for item in expectations if item.authoritative)
        payload = catalog.candidate_types.require(CANDIDATE_TYPE).envelope_from_model(
            CandidatePayload(subject_ref="state:counter")
        )
        return (
            SecurityCandidate.create(
                candidate_type_id=CANDIDATE_TYPE,
                semantic_key="state:counter:delta",
                root_cause=RootCauseRef(
                    owner_bundle_id=BUNDLE_ID,
                    family_id="state_invariant",
                    key="counter-delta",
                ),
                claim_contract=ClaimContract.create(
                    claim_type_id=CANDIDATE_TYPE,
                    title="Counter transition violates the normative baseline",
                    max_claim_status=ClaimStatus.CONFIRMED,
                ),
                max_claim_status=ClaimStatus.CONFIRMED,
                current_claim_status=(
                    ClaimStatus.SUPPORTED if authoritative else ClaimStatus.CANDIDATE
                ),
                candidate_payload=payload,
                grounding_refs=[
                    *(
                        item.expectation_id
                        for item in sorted(expectations, key=lambda x: x.expectation_id)
                    ),
                    "state:counter",
                ],
            ),
        )


@dataclass(frozen=True)
class DeltaInvariantValidator:
    descriptor: ValidatorDescriptor = field(
        default_factory=lambda: ValidatorDescriptor(
            validator_id=VALIDATOR_ID,
            candidate_type_ids=frozenset({CANDIDATE_TYPE}),
            requires_experiment_result=True,
        )
    )

    def evaluate(self, context):
        expectations = tuple(
            item
            for item in context.session_snapshot.expectations.expectations.values()
            if item.expectation_type_id == EXPECTATION_TYPE and item.authoritative
        )
        if not expectations:
            return (
                ValidationDecision(
                    verdict=ValidationVerdict.UNKNOWN,
                    invariant_id="normative-baseline",
                    reason="no authoritative normative baseline is available",
                ),
            )
        selected = sorted(expectations, key=lambda item: item.expectation_id)[0]
        expectation = BaselinePayload.model_validate_json(selected.payload.canonical_payload_json)
        result = context.experiment_result
        assert result is not None and result.result_payload is not None
        observed = DeltaResult.model_validate_json(result.result_payload.canonical_payload_json)
        if observed.before != expectation.expected_before:
            return (
                ValidationDecision(
                    verdict=ValidationVerdict.ANOMALY,
                    invariant_id="baseline-before",
                    reason="observed pre-state differs from the trusted baseline",
                ),
            )
        if observed.after != expectation.expected_after:
            return (
                ValidationDecision(
                    verdict=ValidationVerdict.VIOLATION,
                    invariant_id="normative-after",
                    reason="successful observed transition violates the trusted expected state",
                ),
            )
        return (
            ValidationDecision(
                verdict=ValidationVerdict.PASS,
                invariant_id="normative-after",
                reason="observed transition matches the trusted expected state",
            ),
        )


@dataclass
class CounterExecutor:
    catalog: object
    value: int = 10
    descriptor: ExperimentExecutorDescriptor = field(
        default_factory=lambda: ExperimentExecutorDescriptor(
            executor_id=EXECUTOR_ID,
            action_type_ids=frozenset({ACTION_TYPE}),
            result_type_ids=frozenset({RESULT_TYPE}),
        )
    )

    async def execute(self, descriptor, permit):
        action = MutationAction.model_validate_json(descriptor.input_payload.canonical_payload_json)
        before = self.value
        self.value += action.delta
        payload = self.catalog.experiment_results.require(RESULT_TYPE).envelope_from_model(
            DeltaResult(before=before, after=self.value)
        )
        return ExecutorCompletion(succeeded=True, result_payload=payload)


@dataclass(frozen=True)
class DeltaPolicyEvaluator:
    descriptor: PolicyConstraintEvaluatorDescriptor = field(
        default_factory=lambda: PolicyConstraintEvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID,
            constraint_type_ids=frozenset({CONSTRAINT_TYPE}),
        )
    )

    def evaluate(self, descriptor, constraint):
        action = MutationAction.model_validate_json(descriptor.input_payload.canonical_payload_json)
        allowed = abs(action.delta) <= constraint.max_abs_delta
        return PolicyConstraintDecision(
            allowed=allowed,
            reason_code="DELTA_WITHIN_LIMIT" if allowed else "DELTA_EXCEEDS_LIMIT",
        )
