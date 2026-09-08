from __future__ import annotations

from datetime import datetime

from pydantic import Field

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    CandidateDeriverRegistration,
    DomainBundle,
    ExperimentExecutorRegistration,
    PolicyConstraintEvaluatorRegistration,
    RelationTypeRegistration,
    RequirementTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
    ValidatorRegistration,
    compose_bundles,
)
from redteam_core.investigation import (
    EvidenceFreshness,
    EvidenceRequirement,
    Observation,
    ObservationValidity,
)
from redteam_core.models.base import SchemaModel
from redteam_core.models.provenance import Provenance, TrustClass

FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64


class SamplePayload(SchemaModel):
    value: str = Field(min_length=1, max_length=100)
    enabled: bool = True


class AlternatePayload(SchemaModel):
    count: int = Field(ge=0)


OBSERVATION_TYPE = "reference.observation.value@1"
ALTERNATE_OBSERVATION_TYPE = "reference.observation.alternate@1"
REQUIREMENT_TYPE = "reference.requirement.value@1"


def reference_bundle(*, reverse_schema_order: bool = False) -> DomainBundle:
    registrations = [
        SchemaRegistration(
            category=SchemaCategory.EVIDENCE,
            type_id="reference.evidence.value@1",
            schema_version="1",
            owner_bundle_id="reference",
            model=SamplePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.OBSERVATION_PAYLOAD,
            type_id=OBSERVATION_TYPE,
            schema_version="1",
            owner_bundle_id="reference",
            model=SamplePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.OBSERVATION_PAYLOAD,
            type_id=ALTERNATE_OBSERVATION_TYPE,
            schema_version="1",
            owner_bundle_id="reference",
            model=AlternatePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.EXPECTATION,
            type_id="reference.expectation.value@1",
            schema_version="1",
            owner_bundle_id="reference",
            model=SamplePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.CANDIDATE,
            type_id="reference.candidate.value@1",
            schema_version="1",
            owner_bundle_id="reference",
            model=SamplePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.EXPERIMENT_ACTION,
            type_id="reference.experiment.value@1",
            schema_version="1",
            owner_bundle_id="reference",
            model=SamplePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.EXPERIMENT_RESULT,
            type_id="reference.experiment_result.value@1",
            schema_version="1",
            owner_bundle_id="reference",
            model=AlternatePayload,
            implementation_fingerprint=FP_A,
        ),
        SchemaRegistration(
            category=SchemaCategory.POLICY_CONSTRAINT,
            type_id="reference.policy.value@1",
            schema_version="1",
            owner_bundle_id="reference",
            model=SamplePayload,
            implementation_fingerprint=FP_A,
        ),
    ]
    if reverse_schema_order:
        registrations.reverse()
    return DomainBundle(
        manifest=BundleManifest(
            bundle_id="reference",
            namespace="reference",
            version="1.0.0",
            bundle_kind=BundleKind.REFERENCE,
            implementation_fingerprint=FP_A,
        ),
        schema_registrations=tuple(registrations),
        relation_registrations=(
            RelationTypeRegistration(
                type_id="reference.relation.flows@1",
                schema_version="1",
                owner_bundle_id="reference",
                implementation_fingerprint=FP_A,
            ),
        ),
        requirement_registrations=(
            RequirementTypeRegistration(
                type_id=REQUIREMENT_TYPE,
                schema_version="1",
                owner_bundle_id="reference",
                allowed_observation_payload_type_ids=frozenset({OBSERVATION_TYPE}),
                implementation_fingerprint=FP_A,
            ),
        ),
        candidate_deriver_registrations=(
            CandidateDeriverRegistration(
                deriver_id="reference.deriver.direct@1",
                owner_bundle_id="reference",
                candidate_type_ids=frozenset({"reference.candidate.value@1"}),
                implementation_fingerprint=FP_A,
            ),
        ),
        validator_registrations=(
            ValidatorRegistration(
                validator_id="reference.validator.value@1",
                owner_bundle_id="reference",
                implementation_fingerprint=FP_A,
            ),
        ),
        experiment_executor_registrations=(
            ExperimentExecutorRegistration(
                executor_id="reference.executor.value@1",
                owner_bundle_id="reference",
                action_type_ids=frozenset({"reference.experiment.value@1"}),
                implementation_fingerprint=FP_A,
                result_type_ids=frozenset({"reference.experiment_result.value@1"}),
            ),
        ),
        policy_constraint_evaluator_registrations=(
            PolicyConstraintEvaluatorRegistration(
                constraint_type_id="reference.policy.value@1",
                evaluator_id="reference.policy_evaluator.value@1",
                owner_bundle_id="reference",
                implementation_fingerprint=FP_A,
            ),
        ),
    )


def catalog():
    return compose_bundles((reference_bundle(),))


def requirement(
    *,
    max_age_seconds: int | None = None,
    required_snapshot_ref: str | None = None,
) -> EvidenceRequirement:
    return EvidenceRequirement.create(
        requirement_type_id=REQUIREMENT_TYPE,
        provider_id="reference.local",
        subject_ref="subject:one",
        reason="Need an authoritative observed value.",
        freshness=EvidenceFreshness(
            max_age_seconds=max_age_seconds,
            required_snapshot_ref=required_snapshot_ref,
        ),
    )


def observation(
    *,
    req: EvidenceRequirement,
    value: str,
    acquired_at: datetime,
    resolver_id: str = "reference.resolver.alpha@1",
    trust_class: TrustClass = TrustClass.OBSERVED,
    validity: ObservationValidity | None = None,
) -> Observation:
    cat = catalog()
    registration = cat.observation_payloads.require(OBSERVATION_TYPE)
    envelope = registration.envelope_from_model(SamplePayload(value=value))
    provenance = Provenance(
        source_kind="test",
        producer_id=resolver_id,
        trust_class=trust_class,
        acquired_at=acquired_at,
        content_hash=envelope.content_hash,
        location_ref=req.subject_ref,
    )
    return Observation.create(
        requirement_id=req.requirement_id,
        resolver_id=resolver_id,
        subject_ref=req.subject_ref,
        payload=envelope,
        provenance=provenance,
        validity=validity,
    )
