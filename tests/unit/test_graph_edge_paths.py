"""Paths the rest of the suite never reached — untested branches are where
the next bug hides."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from redteam.attack_graph.identity import (
    credential_node_id,
    is_well_formed,
    make_node_id,
    node_id_for_target,
    node_type_for_target,
    target_for_node,
)
from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    build_properties,
    cleanup_of,
    facts_of,
    merge_properties,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.target import (
    GraphNodeTarget,
    ServiceTarget,
    UrlTarget,
)
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId

NOW = datetime.now(timezone.utc)


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


def host(node_id: str, node_type: GraphNodeType = GraphNodeType.HOST) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=build_properties(),
        created_at=NOW,
        updated_at=NOW,
    )


# -- re-upserting a node's own structured properties ----------------------


async def test_structured_properties_with_an_explicit_step_move_the_window() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"x": True}, step=9)

    await svc.upsert_node(GraphNodeType.HOST, node.properties, node_id=node.id, step=3)

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert refreshed.properties["first_seen_step"] == 3
    assert refreshed.properties["last_seen_step"] == 9


async def test_re_upserting_structured_properties_keeps_the_cleanup_record() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.ASSET, key="tmp-file")
    await svc.register_created_resource(node.id, resource_type="file")
    current = await svc.get_node(node.id)
    assert current is not None

    await svc.upsert_node(GraphNodeType.ASSET, current.properties, node_id=node.id, step=None)

    assert len(await svc.pending_cleanup()) == 1


# -- the search budget backstop -------------------------------------------


def test_a_large_unsatisfiable_edge_search_gives_up_quickly() -> None:
    # Counting cannot rule this out (there are plenty of hosts), so the
    # budget is what keeps it from running forever.
    nodes = [host(f"host:h{index}") for index in range(60)]
    nodes.append(host("principal:p", GraphNodeType.PRINCIPAL))
    patterns = [NodePattern(type=GraphNodeType.PRINCIPAL, bind="p")]
    patterns += [
        NodePattern(
            type=GraphNodeType.HOST,
            bind=f"b{index}",
            count=2,
            related_to="p",
            edge_type=GraphEdgeType.CAN_ACCESS,
        )
        for index in range(10)
    ]

    started = time.perf_counter()
    result = resolve(patterns, nodes=nodes, edges=[])

    assert time.perf_counter() - started < 5.0
    assert not result.ok


def test_an_optional_pattern_with_a_dead_edge_reference_is_dropped_cleanly() -> None:
    # The dropped bind must also disappear from the exclusion table, or a
    # surviving pattern keeps a constraint against something not there.
    nodes = [host("host:a")]
    patterns = [
        NodePattern(type=GraphNodeType.HOST, bind="h"),
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="p", optional=True),
        NodePattern(
            type=GraphNodeType.CREDENTIAL,
            bind="c",
            optional=True,
            related_to="p",
            edge_type=GraphEdgeType.CAN_ACCESS,
            distinct_from=["h"],
        ),
    ]

    result = resolve(patterns, nodes=nodes, edges=[])

    assert result.ok
    assert set(result.bindings) == {"h"}


# -- identity fallbacks ---------------------------------------------------


def test_a_url_without_a_scheme_still_gets_one() -> None:
    node_id = node_id_for_target(UrlTarget(url="api.local/orders/8831"))
    assert "://" in str(node_id)


def test_a_service_without_a_host_round_trips() -> None:
    back = target_for_node(make_node_id(GraphNodeType.SERVICE, "ssh"), GraphNodeType.SERVICE)
    assert isinstance(back, ServiceTarget)
    assert (back.service_name, back.host) == ("ssh", None)


def test_a_hostless_endpoint_falls_back_to_a_url_target() -> None:
    # There is no host for the Control Plane to scope-check, and saying so
    # beats inventing one.
    back = target_for_node(
        make_node_id(GraphNodeType.ENDPOINT, "GET /x"), GraphNodeType.ENDPOINT
    )
    assert isinstance(back, UrlTarget)
    assert back.url == "/x"


def test_a_type_with_no_target_variant_falls_back_to_a_graph_node_target() -> None:
    back = target_for_node(credential_node_id("session-ref-1"), GraphNodeType.CREDENTIAL)
    assert isinstance(back, GraphNodeTarget)
    assert is_well_formed(credential_node_id("session-ref-1"))


def test_a_graph_node_target_without_a_type_reads_as_a_resource() -> None:
    assert (
        node_type_for_target(GraphNodeTarget(node_id=GraphNodeId("host:a")))
        is GraphNodeType.RESOURCE
    )


# -- rendering the cleanup state ------------------------------------------






# -- the read-only view, called directly ----------------------------------


async def test_every_view_method_works_without_going_through_the_service() -> None:
    svc = service()
    view = svc.view()
    host_node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    service_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    edge = await svc.link(GraphEdgeType.HOSTS, host_node.id, service_node.id)

    assert await view.get_node(host_node.id) is not None
    assert await view.get_edge(edge.id) is not None
    assert len(await view.neighbors(host_node.id)) == 1
    assert len(await view.query(node_type=GraphNodeType.HOST)) == 1
    assert len((await view.subgraph_around(host_node.id))[0]) == 2
    assert (await view.cleanup_report()).is_clear
    assert (await view.resolve([NodePattern(type=GraphNodeType.HOST, bind="h")])).ok
    assert (await view.observe()).node_counts == {"host": 1, "service": 1}
    assert (await view.integrity_report()) is not None


# -- merge branches -------------------------------------------------------


def test_cleanup_survives_a_merge_from_either_side() -> None:
    empty = build_properties()
    tracked = build_properties(cleanup={"resource_type": "file", "status": "pending"})

    assert cleanup_of(merge_properties(empty, tracked)) is not None
    assert cleanup_of(merge_properties(tracked, empty)) is not None


def test_a_fact_changing_shape_takes_the_newer_value() -> None:
    merged = merge_properties(
        build_properties(facts={"k": "x"}), build_properties(facts={"k": [1, 2]})
    )
    assert facts_of(merged)["k"] == [1, 2]


def test_only_measured_and_declared_provenance_counts_as_strong() -> None:
    assert Provenance(confidence=Confidence.MEASURED).is_strong
    assert Provenance(confidence=Confidence.SERVER_DECLARED).is_strong
    assert not Provenance().is_strong  # defaults to the weakest grade
    assert not Provenance(confidence=Confidence.ASSERTED).is_strong
