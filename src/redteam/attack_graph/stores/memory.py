"""In-memory ``AttackGraphStore``, with an adjacency index.

Also satisfies the optional ``Listable``/``Mutable`` Protocols.
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
        # Neighbour lookup runs per node on every planning turn — the one
        # hot spot worth indexing.
        self._out: dict[GraphNodeId, set[GraphEdgeId]] = {}
        self._in: dict[GraphNodeId, set[GraphEdgeId]] = {}

    async def add_node(self, node: GraphNode) -> None:
        self._nodes[node.id] = node

    async def add_edge(self, edge: GraphEdge) -> None:
        previous = self._edges.get(edge.id)
        if previous is not None:
            self._out.get(previous.source_id, set()).discard(previous.id)
            self._in.get(previous.target_id, set()).discard(previous.id)
        self._edges[edge.id] = edge
        self._out.setdefault(edge.source_id, set()).add(edge.id)
        self._in.setdefault(edge.target_id, set()).add(edge.id)

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        """Drop a node and every edge touching it."""
        if node_id not in self._nodes:
            return False
        for edge_id in self._out.pop(node_id, set()) | self._in.pop(node_id, set()):
            edge = self._edges.pop(edge_id, None)
            if edge is not None:
                self._out.get(edge.source_id, set()).discard(edge_id)
                self._in.get(edge.target_id, set()).discard(edge_id)
        del self._nodes[node_id]
        return True

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        edge = self._edges.pop(edge_id, None)
        if edge is None:
            return False
        self._out.get(edge.source_id, set()).discard(edge_id)
        self._in.get(edge.target_id, set()).discard(edge_id)
        return True

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return self._nodes.get(node_id)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        return self._edges.get(edge_id)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        incident = self._out.get(node_id, set()) | self._in.get(node_id, set())
        neighbor_ids = {
            self._edges[edge_id].target_id
            if self._edges[edge_id].source_id == node_id
            else self._edges[edge_id].source_id
            for edge_id in incident
        }
        return [self._nodes[nid] for nid in neighbor_ids if nid in self._nodes]

    async def incident_edges(
        self, node_id: GraphNodeId, *, direction: str = "any"
    ) -> list[GraphEdge]:
        """Edges touching one node, without scanning the whole graph."""
        edge_ids: set[GraphEdgeId] = set()
        if direction in ("out", "any"):
            edge_ids |= self._out.get(node_id, set())
        if direction in ("in", "any"):
            edge_ids |= self._in.get(node_id, set())
        return [self._edges[edge_id] for edge_id in sorted(edge_ids, key=str)]

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
