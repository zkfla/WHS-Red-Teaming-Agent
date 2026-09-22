"""In-memory ``AttackGraphStore``.

Sufficient for the skeleton and tests. Swap in a graph-DB-backed store
later behind the same ``redteam.core.ports.attack_graph.AttackGraphStore``
Protocol — explicitly deferred, see instruction.md section 21.
"""

from __future__ import annotations

from typing import Any

from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.ids import GraphEdgeId, GraphNodeId


class InMemoryAttackGraphStore:
    def __init__(self) -> None:
        self._nodes: dict[GraphNodeId, GraphNode] = {}
        self._edges: dict[GraphEdgeId, GraphEdge] = {}

    async def add_node(self, node: GraphNode) -> None:
        self._nodes[node.id] = node

    async def add_edge(self, edge: GraphEdge) -> None:
        self._edges[edge.id] = edge

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return self._nodes.get(node_id)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        return self._edges.get(edge_id)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        neighbor_ids = {
            edge.target_id if edge.source_id == node_id else edge.source_id
            for edge in self._edges.values()
            if node_id in (edge.source_id, edge.target_id)
        }
        return [self._nodes[nid] for nid in neighbor_ids if nid in self._nodes]

    async def query(self, **filters: Any) -> list[GraphNode]:
        node_type = filters.get("node_type")
        results = list(self._nodes.values())
        if node_type is not None:
            results = [n for n in results if n.type == node_type]
        return results
