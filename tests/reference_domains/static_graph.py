from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    CandidateDeriverRegistration,
    DomainBundle,
    RelationTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
)
from redteam_core.graph.frontier import FrontierQuery, build_attack_frontiers
from redteam_core.graph.semantics import GuardEffect, RelationSemantics
from redteam_core.investigation import CandidateDeriverDescriptor
from redteam_core.models.base import SchemaModel
from redteam_core.models.security import (
    ClaimContract,
    ClaimStatus,
    RootCauseRef,
    SecurityCandidate,
)
from tests.reference_domains.common import fp

BUNDLE_ID = "refstatic"
CANDIDATE_TYPE = "refstatic.candidate.flow@1"
DERIVER_ID = "refstatic.deriver.frontier@1"
ENTRY_REL = "refstatic.relation.entry@1"
TRANSFORM_REL = "refstatic.relation.transform@1"
BRIDGE_REL = "refstatic.relation.bridge@1"
SINK_REL = "refstatic.relation.sink@1"
GUARD_REL = "refstatic.relation.guard@1"


class FlowCandidatePayload(SchemaModel):
    frontier_id: str = Field(min_length=1, max_length=200)


def bundle() -> DomainBundle:
    return DomainBundle(
        manifest=BundleManifest(
            bundle_id=BUNDLE_ID,
            namespace=BUNDLE_ID,
            version="1.0.0",
            bundle_kind=BundleKind.REFERENCE,
            implementation_fingerprint=fp("1"),
        ),
        schema_registrations=(
            SchemaRegistration(
                category=SchemaCategory.CANDIDATE,
                type_id=CANDIDATE_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                model=FlowCandidatePayload,
                implementation_fingerprint=fp("2"),
            ),
        ),
        relation_registrations=(
            RelationTypeRegistration(
                type_id=ENTRY_REL,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("3"),
                semantics=RelationSemantics(
                    traversable=True,
                    source_tags=("untrusted",),
                    proves_influence=True,
                ),
            ),
            RelationTypeRegistration(
                type_id=TRANSFORM_REL,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("4"),
                semantics=RelationSemantics(
                    traversable=True,
                    mechanism_tags=("transform",),
                ),
            ),
            RelationTypeRegistration(
                type_id=BRIDGE_REL,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("5"),
                semantics=RelationSemantics(traversable=True, context_bridge=True),
            ),
            RelationTypeRegistration(
                type_id=SINK_REL,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("6"),
                semantics=RelationSemantics(
                    traversable=True,
                    sink_tags=("sensitive",),
                    proves_influence=True,
                ),
            ),
            RelationTypeRegistration(
                type_id=GUARD_REL,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("7"),
                semantics=RelationSemantics(guard_effect=GuardEffect.MAY_BLOCK),
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
class FrontierCandidateDeriver:
    descriptor: CandidateDeriverDescriptor = field(
        default_factory=lambda: CandidateDeriverDescriptor(deriver_id=DERIVER_ID)
    )

    def derive(self, session, catalog):
        search = build_attack_frontiers(
            session,
            catalog=catalog,
            query=FrontierQuery(sink_hints=("sensitive",)),
        )
        registration = catalog.candidate_types.require(CANDIDATE_TYPE)
        contract = ClaimContract.create(
            claim_type_id=CANDIDATE_TYPE,
            title="Synthetic static flow reaches a sensitive sink",
            max_claim_status=ClaimStatus.SUPPORTED,
        )
        candidates = []
        for frontier in search.frontiers:
            if frontier.sink_ref is None:
                continue
            payload = registration.envelope_from_model(
                FlowCandidatePayload(frontier_id=frontier.frontier_id)
            )
            candidates.append(
                SecurityCandidate.create(
                    candidate_type_id=CANDIDATE_TYPE,
                    semantic_key=frontier.frontier_id,
                    root_cause=RootCauseRef(
                        owner_bundle_id=BUNDLE_ID,
                        family_id="flow",
                        key=f"{frontier.source_ref}->{frontier.sink_ref}",
                    ),
                    claim_contract=contract,
                    max_claim_status=ClaimStatus.SUPPORTED,
                    current_claim_status=frontier.claim_status_ceiling,
                    candidate_payload=payload,
                    supporting_relation_ids=list(frontier.relation_ids),
                    guard_relation_ids=list(frontier.guard_relation_ids),
                    requirement_ids=list(frontier.requirement_ids),
                    grounding_refs=[frontier.frontier_id],
                )
            )
        return tuple(candidates)
