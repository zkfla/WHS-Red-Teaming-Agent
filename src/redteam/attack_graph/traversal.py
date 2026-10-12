"""Bounded neighbourhood traversal used by graph context and storage reads."""

from __future__ import annotations

from collections import deque
from typing import Literal

from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType
from redteam.core.ids import GraphNodeId

Direction = Literal["out", "in", "any"]

#: Guard against a pathological graph turning a report into a hang.
DEFAULT_MAX_NODES = 10_000


class Adjacency:
    """Edge index built once per traversal call."""

    __slots__ = ("out", "inn")

    def __init__(self, edges: list[GraphEdge]) -> None:
        self.out: dict[GraphNodeId, list[GraphEdge]] = {}
        self.inn: dict[GraphNodeId, list[GraphEdge]] = {}
        for edge in edges:
            self.out.setdefault(edge.source_id, []).append(edge)
            self.inn.setdefault(edge.target_id, []).append(edge)

    def incident(
        self, node_id: GraphNodeId, direction: Direction
    ) -> list[tuple[GraphEdge, GraphNodeId]]:
        """``(edge, other_end)`` pairs, in a deterministic order."""
        pairs: list[tuple[GraphEdge, GraphNodeId]] = []
        if direction in ("out", "any"):
            pairs.extend((edge, edge.target_id) for edge in self.out.get(node_id, []))
        if direction in ("in", "any"):
            pairs.extend((edge, edge.source_id) for edge in self.inn.get(node_id, []))
        return sorted(pairs, key=lambda pair: (str(pair[0].id), str(pair[1])))


def _index(nodes: list[GraphNode]) -> dict[GraphNodeId, GraphNode]:
    return {node.id: node for node in nodes}


def walk(
    start: GraphNodeId,
    *,
    edges: list[GraphEdge],
    direction: Direction = "out",
    max_depth: int | None = None,
    edge_types: set[GraphEdgeType] | None = None,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> list[tuple[GraphNodeId, GraphEdge | None, int]]:
    """Breadth-first walk. Yields ``(node_id, edge_used, depth)``, start first."""
    adjacency = Adjacency(edges)
    seen = {start}
    order: list[tuple[GraphNodeId, GraphEdge | None, int]] = [(start, None, 0)]
    queue: deque[tuple[GraphNodeId, int]] = deque([(start, 0)])

    while queue:
        if len(order) >= max_nodes:
            break
        node_id, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for edge, other in adjacency.incident(node_id, direction):
            if edge_types is not None and edge.type not in edge_types:
                continue
            if other in seen:
                continue
            # Checked per neighbour, not per dequeue: one node with a
            # thousand neighbours otherwise returns all thousand.
            if len(order) >= max_nodes:
                return order
            seen.add(other)
            order.append((other, edge, depth + 1))
            queue.append((other, depth + 1))
    return order


def subgraph_around(
    center: GraphNodeId,
    *,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    radius: int = 1,
    direction: Direction = "any",
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """The neighbourhood of ``center``, for focused reporting or rendering."""
    visited = walk(center, edges=edges, direction=direction, max_depth=radius)
    kept = {node_id for node_id, _, _ in visited}
    by_id = _index(nodes)
    return (
        [by_id[node_id] for node_id in sorted(kept, key=str) if node_id in by_id],
        [edge for edge in edges if edge.source_id in kept and edge.target_id in kept],
    )
