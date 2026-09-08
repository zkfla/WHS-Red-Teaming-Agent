"""Map primitive test input to registered production types, not oracle output."""

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    DomainBundle,
    RelationTypeRegistration,
    compose_bundles,
)
from redteam_core.graph.frontier import FrontierQuery, build_attack_frontiers
from redteam_core.graph.semantics import GuardEffect, RelationSemantics
from redteam_core.knowledge import CoreSession
from redteam_core.models import Relation, TriState
from tests.performance.oracles import Edge, Guard, Reachability

FP = "sha256:" + "0" * 64


def graph_input(edges: tuple[Edge, ...], guards: tuple[Guard, ...] = ()):
    semantics = {
        "source": RelationSemantics(
            traversable=True,
            source_tags=("input",),
            proves_influence=True,
        ),
        "flow": RelationSemantics(traversable=True, mechanism_tags=("flow",)),
        "bridge": RelationSemantics(traversable=True, context_bridge=True),
        "sink": RelationSemantics(traversable=True, sink_tags=("effect",)),
        **{
            name.lower(): RelationSemantics(guard_effect=GuardEffect(name))
            for name in ("BLOCKS", "MAY_BLOCK", "INFORMATIONAL")
        },
    }
    cat = compose_bundles(
        (
            DomainBundle(
                manifest=BundleManifest(
                    bundle_id="perfref",
                    namespace="perfref",
                    version="1.0.0",
                    bundle_kind=BundleKind.REFERENCE,
                    implementation_fingerprint=FP,
                ),
                relation_registrations=tuple(
                    RelationTypeRegistration(
                        type_id=f"perfref.relation.{role}@1",
                        schema_version="1",
                        owner_bundle_id="perfref",
                        implementation_fingerprint=FP,
                        semantics=value,
                    )
                    for role, value in semantics.items()
                ),
            ),
        )
    )
    relations = [
        Relation.create(
            source_ref=e.source,
            target_ref=e.target,
            relation_type_id=f"perfref.relation.{e.role}@1",
            verdict=TriState(e.verdict),
            context_refs=list(e.contexts),
        )
        for e in edges
    ]
    relations.extend(
        Relation.create(
            source_ref=f"guard:{i}",
            target_ref=g.node,
            relation_type_id=f"perfref.relation.{g.effect.lower()}@1",
            verdict=TriState(g.verdict),
            context_refs=list(g.contexts),
        )
        for i, g in enumerate(guards)
    )
    session = CoreSession()
    session.semantic.relations = {r.relation_id: r for r in relations}
    return session, cat


def core_reachability(
    edges: tuple[Edge, ...], guards: tuple[Guard, ...] = (), *, budget_version: str = "legacy"
):
    session, cat = graph_input(edges, guards)
    result = build_attack_frontiers(
        session,
        catalog=cat,
        query=FrontierQuery(
            max_depth=8,
            max_expansions=100_000,
            max_frontiers=100_000,
            budget_version=budget_version,
            max_work_units=1_000_000,
            max_queue_states=100_000,
            max_aux_visits=100_000,
        ),
    )
    rows: frozenset[Reachability] = frozenset(
        (f.source_ref, f.sink_ref, tuple(sorted(f.context_refs)), f.claim_status_ceiling.value)
        for f in result.frontiers
        if f.sink_ref is not None
    )
    return rows, result
