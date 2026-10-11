"""In-memory ``AttackGraphStore``, with an adjacency index.

Reads hand out copies. Returning the stored object lets any holder —
including code given a read-only ``AttackGraphView`` — edit graph state in
place, skipping validation, merge rules and the secret guard.

Only the mutable parts are copied (``properties``, ``labels``); the rest
of the model is immutable data. That costs roughly 5µs per node, so a
2,000-node ``list_nodes()`` runs in ~10ms rather than being free. It is
the price of the read-only boundary meaning anything.

Also satisfies the optional ``Listable``/``Mutable`` Protocols.
"""

from __future__ import annotations

import copy
from typing import Any

from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationReceipt,
    checked_receipt,
    make_receipt,
    mutation_hash,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId


def _detach_node(node: GraphNode) -> GraphNode:
    """A node the caller may edit without touching what is stored."""
    return node.model_copy(
        update={"properties": copy.deepcopy(node.properties), "labels": list(node.labels)}
    )


def _detach_edge(edge: GraphEdge) -> GraphEdge:
    return edge.model_copy(update={"properties": copy.deepcopy(edge.properties)})


class InMemoryAttackGraphStore:
    def __init__(self) -> None:
        self._nodes: dict[GraphNodeId, GraphNode] = {}
        self._edges: dict[GraphEdgeId, GraphEdge] = {}
        # Neighbour lookup runs per node on every planning turn — the one
        # hot spot worth indexing.
        self._out: dict[GraphNodeId, set[GraphEdgeId]] = {}
        self._in: dict[GraphNodeId, set[GraphEdgeId]] = {}
        self._receipts: dict[tuple[str, str, str], GraphMutationReceipt] = {}

    async def prepare_graph_mutation(self, mutation: GraphMutation) -> GraphMutation:
        return GraphMutation.model_validate(mutation.model_dump()).model_copy(deep=True)

    async def apply_graph_mutation(self, mutation: GraphMutation) -> GraphMutationReceipt:
        # All following memory operations have no suspension point: receipt
        # insertion and the detached graph mutation are one event-loop action.
        mutation = await self.prepare_graph_mutation(mutation)
        mutation_hash(mutation)  # reject unserializable input BEFORE changing the graph
        key = (mutation.namespace, mutation.run_id, mutation.mutation_id)
        previous = self._receipts.get(key)
        if previous is not None:
            return checked_receipt(previous, mutation)
        result: bool | None = None
        if mutation.node is not None:
            self._add_node(mutation.node)
        elif mutation.edge is not None:
            self._add_edge(mutation.edge)
        elif mutation.operation == "remove_node":
            assert mutation.identity is not None
            result = self._remove_node(GraphNodeId(mutation.identity))
        else:
            assert mutation.identity is not None
            result = self._remove_edge(GraphEdgeId(mutation.identity))
        receipt = make_receipt(mutation, result)
        self._receipts[key] = receipt
        return receipt

    async def read_graph_mutation_receipt(
        self, run_id: str, mutation_id: str, *, namespace: str = "",
    ) -> GraphMutationReceipt | None:
        return self._receipts.get((namespace, run_id, mutation_id))

    async def add_node(self, node: GraphNode) -> None:
        self._add_node(node)

    def _add_node(self, node: GraphNode) -> None:
        # Copy on the way in too: the caller keeps a reference to what it
        # passed, and mutating that must not reach back into the store.
        self._nodes[node.id] = _detach_node(node)

    async def add_edge(self, edge: GraphEdge) -> None:
        self._add_edge(edge)

    def _add_edge(self, edge: GraphEdge) -> None:
        previous = self._edges.get(edge.id)
        if previous is not None:
            self._out.get(previous.source_id, set()).discard(previous.id)
            self._in.get(previous.target_id, set()).discard(previous.id)
        self._edges[edge.id] = _detach_edge(edge)
        self._out.setdefault(edge.source_id, set()).add(edge.id)
        self._in.setdefault(edge.target_id, set()).add(edge.id)

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        """Drop a node and every edge touching it."""
        return self._remove_node(node_id)

    def _remove_node(self, node_id: GraphNodeId) -> bool:
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
        return self._remove_edge(edge_id)

    def _remove_edge(self, edge_id: GraphEdgeId) -> bool:
        edge = self._edges.pop(edge_id, None)
        if edge is None:
            return False
        self._out.get(edge.source_id, set()).discard(edge_id)
        self._in.get(edge.target_id, set()).discard(edge_id)
        return True

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        node = self._nodes.get(node_id)
        return None if node is None else _detach_node(node)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        edge = self._edges.get(edge_id)
        return None if edge is None else _detach_edge(edge)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        incident = self._out.get(node_id, set()) | self._in.get(node_id, set())
        neighbor_ids = {
            self._edges[edge_id].target_id
            if self._edges[edge_id].source_id == node_id
            else self._edges[edge_id].source_id
            for edge_id in incident
        }
        return [
            _detach_node(self._nodes[nid])
            for nid in neighbor_ids
            if nid in self._nodes
        ]

    async def incident_edges(
        self, node_id: GraphNodeId, *, direction: str = "any"
    ) -> list[GraphEdge]:
        """Edges touching one node, without scanning the whole graph."""
        edge_ids: set[GraphEdgeId] = set()
        if direction in ("out", "any"):
            edge_ids |= self._out.get(node_id, set())
        if direction in ("in", "any"):
            edge_ids |= self._in.get(node_id, set())
        return [
            _detach_edge(self._edges[edge_id])
            for edge_id in sorted(edge_ids, key=str)
        ]

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
        return [_detach_node(node) for node in results]

    # -- ListableAttackGraphStore ------------------------------------

    async def list_nodes(self) -> list[GraphNode]:
        return [_detach_node(node) for node in self._nodes.values()]

    async def list_edges(self) -> list[GraphEdge]:
        return [_detach_edge(edge) for edge in self._edges.values()]
