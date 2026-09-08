from __future__ import annotations

from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    DomainBundle,
    RelationTypeRegistration,
    compose_bundles,
)
from redteam_core.graph.frontier import (
    FrontierConnectivity,
    FrontierDisposition,
    FrontierQuery,
    build_attack_frontiers,
)
from redteam_core.graph.semantics import GuardEffect, RelationSemantics
from redteam_core.knowledge import CoreSession
from redteam_core.models import ClaimStatus, Relation, TriState
from tests.helpers import FP_A

SOURCE = "graphref.relation.source@1"
SOURCE_ALT = "graphref.relation.source_alt@1"
FLOW = "graphref.relation.flow@1"
BRIDGE = "graphref.relation.bridge@1"
SINK = "graphref.relation.sink@1"
STAGE = "graphref.relation.stage@1"
BLOCK_GUARD = "graphref.relation.block_guard@1"
MAY_GUARD = "graphref.relation.may_guard@1"


def _registration(type_id: str, semantics: RelationSemantics) -> RelationTypeRegistration:
    return RelationTypeRegistration(
        type_id=type_id,
        schema_version="1",
        owner_bundle_id="graphref",
        implementation_fingerprint=FP_A,
        semantics=semantics,
    )


def graph_catalog():
    bundle = DomainBundle(
        manifest=BundleManifest(
            bundle_id="graphref",
            namespace="graphref",
            version="1.0.0",
            bundle_kind=BundleKind.REFERENCE,
            implementation_fingerprint=FP_A,
        ),
        relation_registrations=(
            _registration(
                SOURCE,
                RelationSemantics(
                    traversable=True,
                    source_tags=("external-input",),
                    proves_influence=True,
                ),
            ),
            _registration(
                SOURCE_ALT,
                RelationSemantics(
                    traversable=True,
                    source_tags=("second-input",),
                    proves_influence=True,
                ),
            ),
            _registration(
                FLOW,
                RelationSemantics(
                    traversable=True,
                    mechanism_tags=("propagation",),
                ),
            ),
            _registration(
                BRIDGE,
                RelationSemantics(
                    traversable=True,
                    mechanism_tags=("bridge",),
                    context_bridge=True,
                ),
            ),
            _registration(
                SINK,
                RelationSemantics(
                    traversable=True,
                    sink_tags=("sensitive-effect",),
                ),
            ),
            _registration(
                STAGE,
                RelationSemantics(
                    traversable=True,
                    sink_tags=("intermediate-effect",),
                    continue_after_sink=True,
                ),
            ),
            _registration(
                BLOCK_GUARD,
                RelationSemantics(guard_effect=GuardEffect.BLOCKS),
            ),
            _registration(
                MAY_GUARD,
                RelationSemantics(guard_effect=GuardEffect.MAY_BLOCK),
            ),
        ),
    )
    return compose_bundles((bundle,))


def _session(*relations: Relation) -> CoreSession:
    session = CoreSession()
    session.semantic.relations = {item.relation_id: item for item in relations}
    return session


def _relation(
    source: str,
    type_id: str,
    target: str,
    *,
    verdict: TriState = TriState.TRUE,
    contexts: tuple[str, ...] = (),
) -> Relation:
    return Relation.create(
        source_ref=source,
        relation_type_id=type_id,
        target_ref=target,
        verdict=verdict,
        context_refs=list(contexts),
    )


def test_frontier_finds_continuous_ready_path_without_domain_names():
    source = _relation("principal:x", SOURCE, "node:a", contexts=("ctx:one",))
    flow = _relation("node:a", FLOW, "node:b", contexts=("ctx:one",))
    sink = _relation("node:b", SINK, "resource:z", contexts=("ctx:one",))
    result = build_attack_frontiers(_session(source, flow, sink), catalog=graph_catalog())

    ready = [item for item in result.frontiers if item.proposal_ready]
    assert len(ready) == 1
    frontier = ready[0]
    assert frontier.connectivity is FrontierConnectivity.COMPLETE
    assert frontier.disposition is FrontierDisposition.READY
    assert frontier.claim_status_ceiling is ClaimStatus.SUPPORTED
    assert frontier.relation_ids == (source.relation_id, flow.relation_id, sink.relation_id)
    assert frontier.mechanism_tags == ("propagation",)
    assert frontier.sink_tags == ("sensitive-effect",)
    assert result.search_complete


def test_unknown_relation_caps_frontier_and_preserves_uncertainty():
    source = _relation("principal:x", SOURCE, "node:a")
    flow = _relation("node:a", FLOW, "node:b", verdict=TriState.UNKNOWN)
    sink = _relation("node:b", SINK, "resource:z")
    result = build_attack_frontiers(_session(source, flow, sink), catalog=graph_catalog())
    frontier = next(item for item in result.frontiers if item.sink_ref == "resource:z")

    assert frontier.proposal_ready
    assert frontier.claim_status_ceiling is ClaimStatus.CANDIDATE
    assert flow.relation_id in frontier.unknown_relation_ids


def test_false_relation_is_terminal_blocked():
    source = _relation("principal:x", SOURCE, "node:a")
    flow = _relation("node:a", FLOW, "node:b", verdict=TriState.FALSE)
    sink = _relation("node:b", SINK, "resource:z")
    result = build_attack_frontiers(_session(source, flow, sink), catalog=graph_catalog())

    blocked = [item for item in result.frontiers if item.disposition is FrontierDisposition.BLOCKED]
    assert blocked
    assert not any(item.proposal_ready for item in blocked)
    assert all(item.claim_status_ceiling is ClaimStatus.REJECTED for item in blocked)


def test_registered_guard_semantics_control_blocking_without_name_checks():
    source = _relation("principal:x", SOURCE, "node:a")
    flow = _relation("node:a", FLOW, "node:b")
    sink = _relation("node:b", SINK, "resource:z")
    guard = _relation("guard:q", BLOCK_GUARD, "node:b")
    result = build_attack_frontiers(_session(source, flow, sink, guard), catalog=graph_catalog())
    assert any(item.disposition is FrontierDisposition.BLOCKED for item in result.frontiers)

    unknown_guard = _relation(
        "guard:q",
        MAY_GUARD,
        "node:b",
        verdict=TriState.UNKNOWN,
    )
    result = build_attack_frontiers(
        _session(source, flow, sink, unknown_guard),
        catalog=graph_catalog(),
    )
    frontier = next(item for item in result.frontiers if item.sink_ref == "resource:z")
    assert frontier.claim_status_ceiling is ClaimStatus.CANDIDATE
    assert unknown_guard.relation_id in frontier.uncertain_guard_relation_ids


def test_guard_with_disjoint_context_does_not_block_unrelated_path():
    source = _relation("principal:x", SOURCE, "node:a", contexts=("ctx:one",))
    flow = _relation("node:a", FLOW, "node:b", contexts=("ctx:one",))
    sink = _relation("node:b", SINK, "resource:z", contexts=("ctx:one",))
    guard = _relation("guard:q", BLOCK_GUARD, "node:b", contexts=("ctx:two",))
    result = build_attack_frontiers(_session(source, flow, sink, guard), catalog=graph_catalog())
    frontier = next(item for item in result.frontiers if item.sink_ref == "resource:z")
    assert frontier.proposal_ready
    assert guard.relation_id not in frontier.guard_relation_ids


def test_context_change_requires_explicit_bridge_semantics():
    source = _relation("principal:x", SOURCE, "node:a", contexts=("ctx:one",))
    flow = _relation("node:a", FLOW, "node:b", contexts=("ctx:two",))
    sink = _relation("node:b", SINK, "resource:z", contexts=("ctx:two",))
    blocked = build_attack_frontiers(_session(source, flow, sink), catalog=graph_catalog())
    assert not any(item.sink_ref == "resource:z" for item in blocked.frontiers)

    bridge = _relation("node:a", BRIDGE, "node:b", contexts=("ctx:two",))
    bridged = build_attack_frontiers(_session(source, bridge, sink), catalog=graph_catalog())
    frontier = next(item for item in bridged.frontiers if item.sink_ref == "resource:z")
    assert set(frontier.context_refs) == {"ctx:one", "ctx:two"}


def test_frontier_search_is_deterministic_bounded_and_reports_truncation():
    source = _relation("principal:x", SOURCE, "node:a")
    sink_one = _relation("node:a", SINK, "resource:one")
    sink_two = _relation("node:a", SINK, "resource:two")
    session = _session(source, sink_one, sink_two)
    query = FrontierQuery(max_frontiers=1)
    first = build_attack_frontiers(session, catalog=graph_catalog(), query=query)
    second = build_attack_frontiers(session, catalog=graph_catalog(), query=query)
    assert first == second
    assert first.truncated
    assert first.search_complete
    assert len(first.frontiers) == 1


def test_frontier_diversity_round_robin_preserves_distinct_source_buckets():
    source_one = _relation("principal:one", SOURCE, "node:a")
    source_two = _relation("principal:two", SOURCE_ALT, "node:b")
    sink_one = _relation("node:a", SINK, "resource:one")
    sink_two = _relation("node:a", SINK, "resource:two")
    sink_three = _relation("node:b", SINK, "resource:three")
    result = build_attack_frontiers(
        _session(source_one, source_two, sink_one, sink_two, sink_three),
        catalog=graph_catalog(),
        query=FrontierQuery(source_hints=("external-input",), max_frontiers=2),
    )
    assert len(result.frontiers) == 2
    assert {item.source_tags[0] for item in result.frontiers} == {
        "external-input",
        "second-input",
    }


def test_dominance_prunes_longer_equivalent_route_to_same_state():
    source = _relation("principal:x", SOURCE, "node:a")
    direct = _relation("node:a", FLOW, "node:c")
    detour_one = _relation("node:a", FLOW, "node:b")
    detour_two = _relation("node:b", FLOW, "node:c")
    sink = _relation("node:c", SINK, "resource:z")
    result = build_attack_frontiers(
        _session(source, direct, detour_one, detour_two, sink),
        catalog=graph_catalog(),
    )
    reaching = [item for item in result.frontiers if item.sink_ref == "resource:z"]
    assert len(reaching) == 1
    assert reaching[0].relation_ids == (source.relation_id, direct.relation_id, sink.relation_id)


def test_focused_search_can_continue_past_registered_intermediate_sink():
    source = _relation("principal:x", SOURCE, "node:a")
    stage = _relation("node:a", STAGE, "node:b")
    sink = _relation("node:b", SINK, "resource:z")
    result = build_attack_frontiers(
        _session(source, stage, sink),
        catalog=graph_catalog(),
        query=FrontierQuery(sink_hints=("sensitive-effect",)),
    )
    frontier = next(item for item in result.frontiers if item.sink_ref == "resource:z")
    assert frontier.relation_ids == (source.relation_id, stage.relation_id, sink.relation_id)
    assert frontier.sink_tags == ("sensitive-effect",)


def test_depth_bound_reports_incomplete_search():
    source = _relation("principal:x", SOURCE, "node:a")
    flow = _relation("node:a", FLOW, "node:b")
    sink = _relation("node:b", SINK, "resource:z")
    result = build_attack_frontiers(
        _session(source, flow, sink),
        catalog=graph_catalog(),
        query=FrontierQuery(max_depth=1),
    )
    assert result.depth_limit_reached
    assert not result.search_complete


def test_unregistered_relation_is_fail_closed_non_traversable():
    source = _relation("principal:x", SOURCE, "node:a")
    unknown = _relation("node:a", "other.relation.hidden@1", "resource:z")
    result = build_attack_frontiers(_session(source, unknown), catalog=graph_catalog())
    assert not any(unknown.relation_id in item.relation_ids for item in result.frontiers)
