"""Reachability, paths, and the evidence chain a report is built from."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import build_properties
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.traversal import (
    subgraph_around,
    walk,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
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












def test_a_cycle_does_not_loop_forever() -> None:
    nodes = [node("host:a", GraphNodeType.HOST), node("host:b", GraphNodeType.HOST)]
    edges = [
        edge("host:a", "host:b", GraphEdgeType.RELATED_TO),
        edge("host:b", "host:a", GraphEdgeType.RELATED_TO),
    ]
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










async def test_service_subgraph_returns_nodes_and_edges() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    host = await service.upsert_node(GraphNodeType.HOST, key="a.local")
    svc = await service.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    await service.link(GraphEdgeType.HOSTS, host.id, svc.id)

    nodes, edges = await service.subgraph_around(host.id, radius=1)
    assert len(nodes) == 2 and len(edges) == 1
