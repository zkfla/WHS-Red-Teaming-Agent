from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    CandidateDeriverRegistration,
    DomainBundle,
    RelationTypeRegistration,
    RequirementTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
)
from redteam_core.graph.semantics import RelationSemantics
from redteam_core.investigation import (
    CandidateDeriverDescriptor,
    ResolverDescriptor,
    ResolverOutput,
)
from redteam_core.knowledge import effective_payload_envelope
from redteam_core.models.base import SchemaModel, TriState
from redteam_core.models.security import (
    ClaimContract,
    ClaimStatus,
    Relation,
    RootCauseRef,
    SecurityCandidate,
)
from tests.reference_domains.common import fp

BUNDLE_ID = "refartifact"
OBSERVATION_TYPE = "refartifact.observation.runtime@1"
REQUIREMENT_TYPE = "refartifact.requirement.runtime@1"
RELATION_TYPE = "refartifact.relation.reachable@1"
CANDIDATE_TYPE = "refartifact.candidate.runtime@1"
DERIVER_ID = "refartifact.deriver.runtime@1"
RESOLVER_ID = "refartifact.resolver.fake@1"
RECOMPUTER_ID = "refartifact.recomputer.runtime@1"


class RuntimeCheckPayload(SchemaModel):
    verified: bool


class RuntimeCandidatePayload(SchemaModel):
    subject_ref: str = Field(min_length=1, max_length=200)


def bundle() -> DomainBundle:
    return DomainBundle(
        manifest=BundleManifest(
            bundle_id=BUNDLE_ID,
            namespace=BUNDLE_ID,
            version="1.0.0",
            bundle_kind=BundleKind.REFERENCE,
            implementation_fingerprint=fp("3"),
        ),
        schema_registrations=(
            SchemaRegistration(
                category=SchemaCategory.OBSERVATION_PAYLOAD,
                type_id=OBSERVATION_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                model=RuntimeCheckPayload,
                implementation_fingerprint=fp("4"),
            ),
            SchemaRegistration(
                category=SchemaCategory.CANDIDATE,
                type_id=CANDIDATE_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                model=RuntimeCandidatePayload,
                implementation_fingerprint=fp("5"),
            ),
        ),
        requirement_registrations=(
            RequirementTypeRegistration(
                type_id=REQUIREMENT_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                allowed_observation_payload_type_ids=frozenset({OBSERVATION_TYPE}),
                implementation_fingerprint=fp("6"),
            ),
        ),
        relation_registrations=(
            RelationTypeRegistration(
                type_id=RELATION_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("7"),
                semantics=RelationSemantics(
                    traversable=True,
                    source_tags=("artifact",),
                    sink_tags=("runtime",),
                    proves_influence=True,
                ),
            ),
        ),
        candidate_deriver_registrations=(
            CandidateDeriverRegistration(
                deriver_id=DERIVER_ID,
                owner_bundle_id=BUNDLE_ID,
                candidate_type_ids=frozenset({CANDIDATE_TYPE}),
                implementation_fingerprint=fp("8"),
            ),
        ),
    )


@dataclass(frozen=True)
class RuntimeRecomputer:
    requirement_id: str
    recomputer_id: str = RECOMPUTER_ID

    def recompute(self, session, catalog):
        payload = effective_payload_envelope(session, self.requirement_id)
        verdict = TriState.UNKNOWN
        if payload is not None:
            decoded = RuntimeCheckPayload.model_validate_json(payload.canonical_payload_json)
            verdict = TriState.TRUE if decoded.verified else TriState.FALSE
        return (
            Relation.create(
                source_ref="artifact:entry",
                relation_type_id=RELATION_TYPE,
                target_ref="runtime:sink",
                verdict=verdict,
                requirement_ids=[self.requirement_id],
            ),
        )


@dataclass(frozen=True)
class FakeRuntimeResolver:
    descriptor: ResolverDescriptor = field(
        default_factory=lambda: ResolverDescriptor(
            resolver_id=RESOLVER_ID,
            provider_id="refartifact.fake",
            supported_requirement_type_ids=frozenset({REQUIREMENT_TYPE}),
            trust_rank=90,
            cost_rank=1,
        )
    )

    async def resolve(self, requirement):
        return ResolverOutput(payload=RuntimeCheckPayload(verified=True))


@dataclass(frozen=True)
class RuntimeCandidateDeriver:
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
        payload = catalog.candidate_types.require(CANDIDATE_TYPE).envelope_from_model(
            RuntimeCandidatePayload(subject_ref=relation.source_ref)
        )
        return (
            SecurityCandidate.create(
                candidate_type_id=CANDIDATE_TYPE,
                semantic_key=relation.relation_id,
                root_cause=RootCauseRef(
                    owner_bundle_id=BUNDLE_ID,
                    family_id="static_to_runtime",
                    key=relation.source_ref,
                ),
                claim_contract=ClaimContract.create(
                    claim_type_id=CANDIDATE_TYPE,
                    title="Artifact suspicion is runtime-verifiable",
                    max_claim_status=ClaimStatus.SUPPORTED,
                ),
                max_claim_status=ClaimStatus.SUPPORTED,
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
