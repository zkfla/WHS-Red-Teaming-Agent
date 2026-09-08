from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    CandidateDeriverRegistration,
    DomainBundle,
    RelationTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
    compose_bundles,
)
from redteam_core.graph.frontier import build_attack_frontiers
from redteam_core.graph.semantics import RelationSemantics
from redteam_core.investigation import (
    CandidateDeriverDescriptor,
    CandidateDeriverRegistry,
    RootCauseDecision,
    commit_root_cause_decisions,
    coverage_complete,
    derive_candidates,
    reconcile_candidate_state,
    record_portfolio_selection,
    select_candidate_portfolio,
)
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    Relation,
    RootCauseRef,
    SchemaModel,
    SecurityCandidate,
    TriState,
)
from tests.helpers import FP_A

SOURCE = "batchb.relation.source@1"
FLOW = "batchb.relation.flow@1"
SINK = "batchb.relation.sink@1"
CANDIDATE = "batchb.candidate.path@1"
DERIVER = "batchb.deriver.path@1"


class CandidatePayload(SchemaModel):
    path_kind: str = Field(min_length=1, max_length=80)


def _catalog():
    return compose_bundles(
        (
            DomainBundle(
                manifest=BundleManifest(
                    bundle_id="batchb",
                    namespace="batchb",
                    version="1.0.0",
                    bundle_kind=BundleKind.REFERENCE,
                    implementation_fingerprint=FP_A,
                ),
                schema_registrations=(
                    SchemaRegistration(
                        category=SchemaCategory.CANDIDATE,
                        type_id=CANDIDATE,
                        schema_version="1",
                        owner_bundle_id="batchb",
                        model=CandidatePayload,
                        implementation_fingerprint=FP_A,
                    ),
                ),
                relation_registrations=(
                    RelationTypeRegistration(
                        type_id=SOURCE,
                        schema_version="1",
                        owner_bundle_id="batchb",
                        implementation_fingerprint=FP_A,
                        semantics=RelationSemantics(
                            traversable=True,
                            source_tags=("untrusted-origin",),
                            proves_influence=True,
                        ),
                    ),
                    RelationTypeRegistration(
                        type_id=FLOW,
                        schema_version="1",
                        owner_bundle_id="batchb",
                        implementation_fingerprint=FP_A,
                        semantics=RelationSemantics(
                            traversable=True,
                            mechanism_tags=("data-flow",),
                        ),
                    ),
                    RelationTypeRegistration(
                        type_id=SINK,
                        schema_version="1",
                        owner_bundle_id="batchb",
                        implementation_fingerprint=FP_A,
                        semantics=RelationSemantics(
                            traversable=True,
                            sink_tags=("sensitive-operation",),
                        ),
                    ),
                ),
                candidate_deriver_registrations=(
                    CandidateDeriverRegistration(
                        deriver_id=DERIVER,
                        owner_bundle_id="batchb",
                        candidate_type_ids=frozenset({CANDIDATE}),
                        implementation_fingerprint=FP_A,
                    ),
                ),
            ),
        )
    )


@dataclass
class PathDeriver:
    descriptor: CandidateDeriverDescriptor = CandidateDeriverDescriptor(deriver_id=DERIVER)

    def derive(self, session: CoreSession, catalog) -> tuple[SecurityCandidate, ...]:
        relation_ids = tuple(sorted(session.semantic.relations))
        sink_relations = [
            relation
            for relation in session.semantic.relations.values()
            if relation.relation_type_id == SINK and relation.verdict is TriState.TRUE
        ]
        if not sink_relations:
            return ()
        contract = ClaimContract.create(
            claim_type_id=CANDIDATE,
            title="Reference graph candidate",
            max_claim_status=ClaimStatus.SUPPORTED,
        )
        payload_registration = catalog.candidate_types.require(CANDIDATE)
        payload = payload_registration.envelope_from_model(CandidatePayload(path_kind="graph"))
        return (
            SecurityCandidate.create(
                candidate_type_id=CANDIDATE,
                semantic_key="path:one",
                root_cause=RootCauseRef(
                    owner_bundle_id="batchb",
                    family_id="graph-flow",
                    key="sensitive-operation",
                ),
                claim_contract=contract,
                max_claim_status=ClaimStatus.SUPPORTED,
                current_claim_status=ClaimStatus.SUPPORTED,
                candidate_payload=payload,
                supporting_relation_ids=list(relation_ids),
            ),
        )


def test_batch_b_graph_to_candidate_to_terminal_coverage_spine():
    catalog = _catalog()
    session = CoreSession()
    relations = (
        Relation.create(
            source_ref="principal:external",
            relation_type_id=SOURCE,
            target_ref="node:input",
            verdict=TriState.TRUE,
        ),
        Relation.create(
            source_ref="node:input",
            relation_type_id=FLOW,
            target_ref="node:operation",
            verdict=TriState.TRUE,
        ),
        Relation.create(
            source_ref="node:operation",
            relation_type_id=SINK,
            target_ref="resource:sensitive",
            verdict=TriState.TRUE,
        ),
    )
    session.semantic.relations = {relation.relation_id: relation for relation in relations}

    frontier_result = build_attack_frontiers(session, catalog=catalog)
    frontier = next(item for item in frontier_result.frontiers if item.proposal_ready)
    assert frontier.sink_ref == "resource:sensitive"
    assert frontier.claim_status_ceiling is ClaimStatus.SUPPORTED

    derivers = CandidateDeriverRegistry()
    derivers.register(PathDeriver())
    derivers.freeze()
    candidates = derive_candidates(session, catalog=catalog, registry=derivers)
    assert len(candidates) == 1
    assert candidates[0].supporting_relation_ids == sorted(session.semantic.relations)

    reconcile_candidate_state(session, candidates)
    portfolio = select_candidate_portfolio(session, limit=6)
    assert len(portfolio.entries) == 1
    record_portfolio_selection(session, portfolio)
    commit_root_cause_decisions(
        session,
        portfolio,
        (
            RootCauseDecision(
                root_cause_key=portfolio.entries[0].root_cause_key,
                disposition=CandidateDisposition.ACCEPTED,
                reason="reference terminal decision",
            ),
        ),
    )
    assert coverage_complete(session)
    assert next(iter(session.candidate_state.candidates.values())).disposition is (
        CandidateDisposition.ACCEPTED
    )
