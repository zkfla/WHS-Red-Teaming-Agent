"""Attack Graph Test: nodes/edges can be added and traversed."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId


def _node(node_id: str, node_type: GraphNodeType, now: datetime) -> GraphNode:
    return GraphNode(id=GraphNodeId(node_id), type=node_type, created_at=now, updated_at=now)


async def test_add_and_get_node() -> None:
    store = InMemoryAttackGraphStore()
    now = datetime.now(timezone.utc)
    node = _node("host_1", GraphNodeType.HOST, now)
    await store.add_node(node)

    fetched = await store.get_node(GraphNodeId("host_1"))
    assert fetched == node


async def test_neighbors_via_edge() -> None:
    store = InMemoryAttackGraphStore()
    now = datetime.now(timezone.utc)
    host = _node("host_1", GraphNodeType.HOST, now)
    service = _node("svc_1", GraphNodeType.SERVICE, now)
    await store.add_node(host)
    await store.add_node(service)
    edge = GraphEdge(
        id=GraphEdgeId("edge_1"),
        type=GraphEdgeType.HOSTS,
        source_id=host.id,
        target_id=service.id,
        created_at=now,
    )
    await store.add_edge(edge)

    neighbors = await store.neighbors(host.id)
    assert neighbors == [service]

    fetched_edge = await store.get_edge(GraphEdgeId("edge_1"))
    assert fetched_edge == edge


async def test_get_edge_missing_returns_none() -> None:
    store = InMemoryAttackGraphStore()
    assert await store.get_edge(GraphEdgeId("nope")) is None


async def test_query_filters_by_node_type() -> None:
    store = InMemoryAttackGraphStore()
    now = datetime.now(timezone.utc)
    await store.add_node(_node("host_1", GraphNodeType.HOST, now))
    await store.add_node(_node("svc_1", GraphNodeType.SERVICE, now))

    hosts = await store.query(node_type=GraphNodeType.HOST)
    assert [n.id for n in hosts] == [GraphNodeId("host_1")]
