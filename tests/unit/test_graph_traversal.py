"""Reachability, paths, and the evidence chain a report is built from."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import build_properties
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.traversal import (
    ancestors,
    descendants,
    shortest_path,
    subgraph_around,
    walk,
)
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId

NOW = datetime.now(timezone.utc)


def node(node_id: str, node_type: GraphNodeType, step: int = 0) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=build_properties(step=step),
        created_at=NOW,
        updated_at=NOW,
    )


def edge(source: str, target: str, edge_type: GraphEdgeType) -> GraphEdge:
    return GraphEdge(
        id=GraphEdgeId(f"{edge_type.value}:{source}->{target}"),
        type=edge_type,
        source_id=GraphNodeId(source),
        target_id=GraphNodeId(target),
        created_at=NOW,
    )


def chain_fixture() -> tuple[list[GraphNode], list[GraphEdge]]:
    """host <- service <- endpoint, with a hypothesis and finding on top."""
    nodes = [
        node("host:a.local", GraphNodeType.HOST),
        node("service:a.local/http", GraphNodeType.SERVICE),
        node("endpoint:a.local GET /admin", GraphNodeType.ENDPOINT),
    ]
    edges = [
        edge("host:a.local", "service:a.local/http", GraphEdgeType.HOSTS),
        edge("service:a.local/http", "endpoint:a.local GET /admin", GraphEdgeType.EXPOSES),
    ]
    return nodes, edges


def test_walk_returns_start_first_and_records_depth() -> None:
    nodes, edges = chain_fixture()
    visited = walk(GraphNodeId("host:a.local"), edges=edges)
    assert [str(nid) for nid, _, _ in visited] == [
        "host:a.local",
        "service:a.local/http",
        "endpoint:a.local GET /admin",
    ]
    assert [depth for _, _, depth in visited] == [0, 1, 2]


def test_descendants_and_ancestors_are_opposite_directions() -> None:
    _, edges = chain_fixture()
    assert descendants(GraphNodeId("host:a.local"), edges=edges) == {
        GraphNodeId("service:a.local/http"),
        GraphNodeId("endpoint:a.local GET /admin"),
    }
    assert ancestors(GraphNodeId("endpoint:a.local GET /admin"), edges=edges) == {
        GraphNodeId("service:a.local/http"),
        GraphNodeId("host:a.local"),
    }
    assert descendants(GraphNodeId("endpoint:a.local GET /admin"), edges=edges) == set()


def test_max_depth_bounds_the_walk() -> None:
    _, edges = chain_fixture()
    assert descendants(GraphNodeId("host:a.local"), edges=edges, max_depth=1) == {
        GraphNodeId("service:a.local/http")
    }


def test_shortest_path_returns_the_edges_used() -> None:
    _, edges = chain_fixture()
    path = shortest_path(
        GraphNodeId("host:a.local"), GraphNodeId("endpoint:a.local GET /admin"), edges=edges
    )
    assert path is not None
    assert [e.type for e in path] == [GraphEdgeType.HOSTS, GraphEdgeType.EXPOSES]


def test_shortest_path_is_none_when_unlinked_and_empty_for_self() -> None:
    _, edges = chain_fixture()
    here = GraphNodeId("host:a.local")
    assert shortest_path(here, here, edges=edges) == []
    assert (
        shortest_path(
            GraphNodeId("endpoint:a.local GET /admin"), GraphNodeId("host:a.local"), edges=edges
        )
        is None  # wrong direction
    )
    assert shortest_path(
        GraphNodeId("host:a.local"), GraphNodeId("host:nope"), edges=edges
    ) is None


def test_edge_type_filter_restricts_the_walk() -> None:
    _, edges = chain_fixture()
    assert descendants(
        GraphNodeId("host:a.local"), edges=edges, edge_types={GraphEdgeType.HOSTS}
    ) == {GraphNodeId("service:a.local/http")}


def test_a_cycle_does_not_loop_forever() -> None:
    nodes = [node("host:a", GraphNodeType.HOST), node("host:b", GraphNodeType.HOST)]
    edges = [
        edge("host:a", "host:b", GraphEdgeType.RELATED_TO),
        edge("host:b", "host:a", GraphEdgeType.RELATED_TO),
    ]
    assert descendants(GraphNodeId("host:a"), edges=edges) == {GraphNodeId("host:b")}
    assert len(subgraph_around(GraphNodeId("host:a"), nodes=nodes, edges=edges, radius=5)[0]) == 2


def test_subgraph_around_keeps_only_internal_edges() -> None:
    nodes, edges = chain_fixture()
    kept_nodes, kept_edges = subgraph_around(
        GraphNodeId("service:a.local/http"), nodes=nodes, edges=edges, radius=1
    )
    assert len(kept_nodes) == 3
    assert len(kept_edges) == 2

    kept_nodes, kept_edges = subgraph_around(
        GraphNodeId("host:a.local"), nodes=nodes, edges=edges, radius=1
    )
    assert {str(n.id) for n in kept_nodes} == {"host:a.local", "service:a.local/http"}
    assert [e.type for e in kept_edges] == [GraphEdgeType.HOSTS]


# -- evidence chain -------------------------------------------------------


async def built_finding() -> AttackGraphService:
    service = AttackGraphService(InMemoryAttackGraphStore())
    host = await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await service.record_hypothesis(
        Hypothesis(hypothesis_id="hyp_1", description="admin page is world-readable"),
        about=host.id,
        step=1,
    )
    await service.record_finding(
        Finding(
            finding_id="find_1",
            title="anonymous admin read",
            severity="high",
            verified=True,
            evidence_ids=["ev_1"],
            metadata={"action_id": "action_1"},
        ),
        hypothesis_id="hyp_1",
        about=host.id,
        step=2,
    )
    return service


async def test_evidence_chain_reconstructs_finding_to_target() -> None:
    service = await built_finding()

    chain = await service.evidence_chain("find_1")

    assert chain is not None
    assert chain.finding_id == "find_1"
    assert chain.severity == "high"
    assert chain.evidence_ids == ["ev_1"]
    assert chain.hypothesis_ids == ["hyp_1"]
    assert chain.action_ids == ["action_1"]
    assert chain.is_grounded
    # finding -> (supported_by) hypothesis, and -> (targets) the host
    types = [link.node_type.value for link in chain.links]
    assert types[0] == "finding"
    assert set(types[1:]) == {"hypothesis", "host"}
    assert "find_1" in chain.describe() and "ev_1" in chain.describe()


async def test_evidence_chain_is_none_for_a_non_finding() -> None:
    service = await built_finding()
    assert await service.evidence_chain("nope") is None


async def test_evidence_chains_are_sorted_worst_first() -> None:
    service = await built_finding()
    await service.record_finding(
        Finding(finding_id="find_2", title="low one", severity="low", verified=False)
    )
    assert [c.finding_id for c in await service.evidence_chains()] == ["find_1", "find_2"]


async def test_service_traversal_helpers_return_nodes() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    host = await service.upsert_node(GraphNodeType.HOST, key="a.local")
    svc = await service.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    await service.link(GraphEdgeType.HOSTS, host.id, svc.id)

    assert [n.id for n in await service.descendants(host.id)] == [svc.id]
    assert [n.id for n in await service.ancestors(svc.id)] == [host.id]
    path = await service.shortest_path(host.id, svc.id)
    assert path is not None and len(path) == 1
    nodes, edges = await service.subgraph_around(host.id, radius=1)
    assert len(nodes) == 2 and len(edges) == 1
