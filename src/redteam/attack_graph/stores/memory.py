"""In-memory ``AttackGraphStore``.

Also satisfies ``stores.base.ListableAttackGraphStore``. A graph-DB
backend goes behind the same Protocol later (instruction.md section 21).
"""

from __future__ import annotations

from typing import Any

from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType
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
        """Keyword filters: ``node_type``, ``label``, ``attrs`` (subset
        match). Unknown filters raise rather than matching everything."""
        results = list(self._nodes.values())

        node_type = filters.pop("node_type", None)
        if node_type is not None:
            if isinstance(node_type, str):
                node_type = GraphNodeType(node_type)
            results = [node for node in results if node.type == node_type]

        label = filters.pop("label", None)
        if label is not None:
            results = [node for node in results if label in node.labels]

        attrs = filters.pop("attrs", None)
        if attrs:
            results = [
                node
                for node in results
                if all(
                    node.properties.get("attrs", {}).get(key) == value
                    for key, value in attrs.items()
                )
            ]

        if filters:
            raise TypeError(f"unsupported query filters: {sorted(filters)}")
        return results

    # -- ListableAttackGraphStore ------------------------------------

    async def list_nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    async def list_edges(self) -> list[GraphEdge]:
        return list(self._edges.values())
