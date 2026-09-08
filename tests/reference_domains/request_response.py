from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field

from redteam_core.execution import ExecutorCompletion, ExperimentExecutorDescriptor
from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    CandidateDeriverRegistration,
    DomainBundle,
    ExperimentExecutorRegistration,
    RelationTypeRegistration,
    RequirementTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
    ValidatorRegistration,
)
from redteam_core.graph.semantics import RelationSemantics
from redteam_core.investigation import (
    CandidateDeriverDescriptor,
    EvidenceResolutionError,
    ResolutionFailureKind,
    ResolverDescriptor,
)
from redteam_core.knowledge import effective_payload_envelope
from redteam_core.models.base import SchemaModel, TriState
from redteam_core.models.security import (
    ClaimContract,
    ClaimStatus,
    Relation,
    RootCauseRef,
    SecurityCandidate,
    ValidationVerdict,
)
from redteam_core.validation import ValidationDecision, ValidatorDescriptor
from tests.reference_domains.common import fp

BUNDLE_ID = "refrequest"
OBSERVATION_TYPE = "refrequest.observation.access@1"
REQUIREMENT_TYPE = "refrequest.requirement.authorization@1"
RELATION_TYPE = "refrequest.relation.cross_access@1"
CANDIDATE_TYPE = "refrequest.candidate.authorization@1"
READ_ACTION = "refrequest.experiment.read_cross@1"
WRITE_ACTION = "refrequest.experiment.write_cross@1"
RESULT_TYPE = "refrequest.result.access@1"
DERIVER_ID = "refrequest.deriver.authorization@1"
VALIDATOR_ID = "refrequest.validator.authorization@1"
EXECUTOR_ID = "refrequest.executor.fake@1"
RESOLVER_ID = "refrequest.resolver.not_found@1"
RECOMPUTER_ID = "refrequest.recomputer.authorization@1"


class AccessObservation(SchemaModel):
    granted: bool
    response_code: int = Field(ge=100, le=599)


class AuthorizationCandidatePayload(SchemaModel):
    resource_ref: str = Field(min_length=1, max_length=200)


class RequestAction(SchemaModel):
    resource_ref: str = Field(min_length=1, max_length=200)


class AccessResult(SchemaModel):
    granted: bool
    response_code: int = Field(ge=100, le=599)


def bundle() -> DomainBundle:
    schemas = (
        (SchemaCategory.OBSERVATION_PAYLOAD, OBSERVATION_TYPE, AccessObservation, "3"),
        (SchemaCategory.CANDIDATE, CANDIDATE_TYPE, AuthorizationCandidatePayload, "4"),
        (SchemaCategory.EXPERIMENT_ACTION, READ_ACTION, RequestAction, "5"),
        (SchemaCategory.EXPERIMENT_ACTION, WRITE_ACTION, RequestAction, "6"),
        (SchemaCategory.EXPERIMENT_RESULT, RESULT_TYPE, AccessResult, "7"),
    )
    return DomainBundle(
        manifest=BundleManifest(
            bundle_id=BUNDLE_ID,
            namespace=BUNDLE_ID,
            version="1.0.0",
            bundle_kind=BundleKind.REFERENCE,
            implementation_fingerprint=fp("8"),
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
        requirement_registrations=(
            RequirementTypeRegistration(
                type_id=REQUIREMENT_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                allowed_observation_payload_type_ids=frozenset({OBSERVATION_TYPE}),
                implementation_fingerprint=fp("9"),
            ),
        ),
        relation_registrations=(
            RelationTypeRegistration(
                type_id=RELATION_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("a"),
                semantics=RelationSemantics(),
            ),
        ),
        candidate_deriver_registrations=(
            CandidateDeriverRegistration(
                deriver_id=DERIVER_ID,
                owner_bundle_id=BUNDLE_ID,
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                implementation_fingerprint=fp("b"),
            ),
        ),
        validator_registrations=(
            ValidatorRegistration(
                validator_id=VALIDATOR_ID,
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("c"),
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                requires_experiment_result=True,
            ),
        ),
        experiment_executor_registrations=(
            ExperimentExecutorRegistration(
                executor_id=EXECUTOR_ID,
                owner_bundle_id=BUNDLE_ID,
                action_type_ids=frozenset({READ_ACTION, WRITE_ACTION}),
                result_type_ids=frozenset({RESULT_TYPE}),
                implementation_fingerprint=fp("d"),
            ),
        ),
    )


@dataclass(frozen=True)
class AuthorizationRecomputer:
    requirement_id: str
    recomputer_id: str = RECOMPUTER_ID

    def recompute(self, session, catalog):
        payload = effective_payload_envelope(session, self.requirement_id)
        verdict = TriState.UNKNOWN
        if payload is not None:
            access = AccessObservation.model_validate_json(payload.canonical_payload_json)
            verdict = TriState.TRUE if access.granted else TriState.FALSE
        return (
            Relation.create(
                source_ref="principal:attacker",
                relation_type_id=RELATION_TYPE,
                target_ref="resource:other-owner",
                verdict=verdict,
                requirement_ids=[self.requirement_id],
            ),
        )


@dataclass(frozen=True)
class NotFoundResolver:
    descriptor: ResolverDescriptor = field(
        default_factory=lambda: ResolverDescriptor(
            resolver_id=RESOLVER_ID,
            provider_id="refrequest.fake",
            supported_requirement_type_ids=frozenset({REQUIREMENT_TYPE}),
        )
    )

    async def resolve(self, requirement):
        raise EvidenceResolutionError(
            ResolutionFailureKind.AMBIGUOUS_NOT_FOUND,
            "synthetic 404 does not prove authorization safety",
        )


@dataclass(frozen=True)
class AuthorizationDeriver:
    descriptor: CandidateDeriverDescriptor = field(
        default_factory=lambda: CandidateDeriverDescriptor(deriver_id=DERIVER_ID)
    )

    def derive(self, session, catalog):
        relations = tuple(
            item
            for item in session.semantic.relations.values()
            if item.relation_type_id == RELATION_TYPE
        )
        if not relations:
            return ()
        relation = sorted(relations, key=lambda item: item.relation_id)[0]
        if relation.verdict is TriState.FALSE:
            return ()
        payload = catalog.candidate_types.require(CANDIDATE_TYPE).envelope_from_model(
            AuthorizationCandidatePayload(resource_ref=relation.target_ref)
        )
        return (
            SecurityCandidate.create(
                candidate_type_id=CANDIDATE_TYPE,
                semantic_key=relation.relation_id,
                root_cause=RootCauseRef(
                    owner_bundle_id=BUNDLE_ID,
                    family_id="authorization",
                    key=relation.target_ref,
                ),
                claim_contract=ClaimContract.create(
                    claim_type_id=CANDIDATE_TYPE,
                    title="Cross-principal resource access may be authorized incorrectly",
                    max_claim_status=ClaimStatus.CONFIRMED,
                ),
                max_claim_status=ClaimStatus.CONFIRMED,
                current_claim_status=(
                    ClaimStatus.SUPPORTED
                    if relation.verdict is TriState.TRUE
                    else ClaimStatus.CANDIDATE
                ),
                candidate_payload=payload,
                supporting_relation_ids=[relation.relation_id],
                requirement_ids=list(relation.requirement_ids),
                grounding_refs=[relation.relation_id],
            ),
        )


@dataclass(frozen=True)
class AuthorizationValidator:
    descriptor: ValidatorDescriptor = field(
        default_factory=lambda: ValidatorDescriptor(
            validator_id=VALIDATOR_ID,
            candidate_type_ids=frozenset({CANDIDATE_TYPE}),
            requires_experiment_result=True,
        )
    )

    def evaluate(self, context):
        result = context.experiment_result
        assert result is not None and result.result_payload is not None
        access = AccessResult.model_validate_json(result.result_payload.canonical_payload_json)
        return (
            ValidationDecision(
                verdict=(
                    ValidationVerdict.VIOLATION if access.granted else ValidationVerdict.PASS
                ),
                invariant_id="cross-principal-authorization",
                reason=(
                    "cross-principal access was granted"
                    if access.granted
                    else "cross-principal access was denied"
                ),
            ),
        )


@dataclass
class FakeRequestExecutor:
    catalog: object
    descriptor: ExperimentExecutorDescriptor = field(
        default_factory=lambda: ExperimentExecutorDescriptor(
            executor_id=EXECUTOR_ID,
            action_type_ids=frozenset({READ_ACTION, WRITE_ACTION}),
            result_type_ids=frozenset({RESULT_TYPE}),
        )
    )

    async def execute(self, descriptor, permit):
        payload = self.catalog.experiment_results.require(RESULT_TYPE).envelope_from_model(
            AccessResult(granted=True, response_code=200)
        )
        return ExecutorCompletion(succeeded=True, result_payload=payload)
