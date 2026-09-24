"""Walking the graph: reachability, paths, and evidence chains.

``evidence_chain`` is the one a report needs — it walks a Finding back to
the Hypothesis it came from and the target it was about.
"""

from __future__ import annotations

from collections import deque
from typing import Literal

from pydantic import BaseModel, Field

from redteam.attack_graph.predicates import facts_of, last_seen_step, predicate_flags
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
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


def reachable_from(
    start: GraphNodeId,
    *,
    edges: list[GraphEdge],
    direction: Direction = "out",
    max_depth: int | None = None,
    edge_types: set[GraphEdgeType] | None = None,
) -> set[GraphNodeId]:
    """Every node reachable from ``start``, excluding ``start`` itself."""
    visited = walk(
        start, edges=edges, direction=direction, max_depth=max_depth, edge_types=edge_types
    )
    return {node_id for node_id, _, depth in visited if depth > 0}


def descendants(start: GraphNodeId, *, edges: list[GraphEdge], **kwargs) -> set[GraphNodeId]:
    return reachable_from(start, edges=edges, direction="out", **kwargs)


def ancestors(start: GraphNodeId, *, edges: list[GraphEdge], **kwargs) -> set[GraphNodeId]:
    return reachable_from(start, edges=edges, direction="in", **kwargs)


def shortest_path(
    source: GraphNodeId,
    target: GraphNodeId,
    *,
    edges: list[GraphEdge],
    direction: Direction = "out",
    edge_types: set[GraphEdgeType] | None = None,
) -> list[GraphEdge] | None:
    """The edges linking ``source`` to ``target``, or ``None`` if unlinked."""
    if source == target:
        return []

    adjacency = Adjacency(edges)
    came_from: dict[GraphNodeId, tuple[GraphNodeId, GraphEdge]] = {}
    seen = {source}
    queue: deque[GraphNodeId] = deque([source])

    while queue:
        node_id = queue.popleft()
        for edge, other in adjacency.incident(node_id, direction):
            if edge_types is not None and edge.type not in edge_types:
                continue
            if other in seen:
                continue
            seen.add(other)
            came_from[other] = (node_id, edge)
            if other == target:
                path: list[GraphEdge] = []
                cursor = target
                while cursor != source:
                    previous, used = came_from[cursor]
                    path.append(used)
                    cursor = previous
                return list(reversed(path))
            queue.append(other)
    return None


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


# -- the chain a report is built from -------------------------------------


class ChainLink(BaseModel):
    """One hop of an evidence chain."""

    node_id: GraphNodeId
    node_type: GraphNodeType
    label: str
    via: GraphEdgeType | None = None
    step: int = 0
    #: What this hop was reached from. Without it a chain reads as one
    #: line, so a Finding pointing at two separate things is described as
    #: a path through both — a route that does not exist in the graph.
    parent_id: GraphNodeId | None = None
    depth: int = 0

    def describe(self) -> str:
        arrow = f" --{self.via.value}--> " if self.via else ""
        return f"{arrow}{self.node_type.value}:{self.label}"


class EvidenceChain(BaseModel):
    """How a Finding came to be: hypothesis, target, and the evidence behind it."""

    finding_id: str
    severity: str = "info"
    verified: bool = False
    links: list[ChainLink] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    #: The walk hit its depth limit, so this chain is incomplete.
    truncated: bool = False

    @property
    def is_grounded(self) -> bool:
        """A verified finding with no evidence cannot be reported."""
        return bool(self.evidence_ids) if self.verified else True

    @property
    def branches(self) -> list[list[ChainLink]]:
        """Each root-to-leaf path, so a fork is not flattened into a line."""
        children: dict[GraphNodeId | None, list[ChainLink]] = {}
        for link in self.links:
            children.setdefault(link.parent_id, []).append(link)

        roots = [link for link in self.links if link.parent_id is None]
        paths: list[list[ChainLink]] = []

        def walk_down(link: ChainLink, prefix: list[ChainLink]) -> None:
            trail = [*prefix, link]
            kids = children.get(link.node_id, [])
            if not kids:
                paths.append(trail)
                return
            for kid in kids:
                walk_down(kid, trail)

        for root in roots:
            walk_down(root, [])
        return paths

    def describe(self) -> str:
        suffix = " …(truncated)" if self.truncated else ""
        rendered = [
            "".join(link.describe() for link in branch) for branch in self.branches
        ]
        path = " | ".join(rendered) if rendered else ""
        return (
            f"{self.finding_id} [{self.severity}] {path}{suffix} "
            f"(evidence: {', '.join(self.evidence_ids) or 'none'})"
        )


def _label(node: GraphNode) -> str:
    _, _, key = str(node.id).partition(":")
    return f"{key or node.id}{predicate_flags(node.properties)}"


def evidence_chain(
    finding_node_id: GraphNodeId,
    *,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    max_depth: int = 8,
) -> EvidenceChain | None:
    """Walk out from a Finding to what supports it, following the edges
    VERIFY/PLAN write and collecting the correlation ids alongside."""
    by_id = _index(nodes)
    finding_node = by_id.get(finding_node_id)
    if finding_node is None or finding_node.type is not GraphNodeType.FINDING:
        return None

    finding_facts = facts_of(finding_node.properties)
    chain = EvidenceChain(
        finding_id=str(finding_facts.get("finding_id", finding_node_id)),
        severity=str(finding_facts.get("severity", "info")),
        verified=bool(finding_facts.get("verified", False)),
        evidence_ids=[str(item) for item in finding_facts.get("evidence_ids", []) or []],
    )

    action_id = finding_facts.get("metadata", {}).get("action_id")
    if action_id:
        chain.action_ids.append(str(action_id))

    visited = walk(finding_node_id, edges=edges, direction="out", max_depth=max_depth)
    reached = {node_id for node_id, _, _ in visited}
    # An edge leaving the deepest kept node means the walk was cut short.
    deepest = {node_id for node_id, _, depth in visited if depth == max_depth}
    chain.truncated = any(
        edge.source_id in deepest and edge.target_id not in reached for edge in edges
    )

    for node_id, edge, depth in visited:
        node = by_id.get(node_id)
        if node is None:
            continue
        chain.links.append(
            ChainLink(
                node_id=node_id,
                node_type=node.type,
                label=_label(node),
                via=edge.type if edge is not None else None,
                step=last_seen_step(node.properties),
                parent_id=edge.source_id if edge is not None else None,
                depth=depth,
            )
        )
        if node.type is GraphNodeType.HYPOTHESIS:
            hypothesis_facts = facts_of(node.properties)
            hypothesis_id = hypothesis_facts.get("hypothesis_id")
            if hypothesis_id:
                chain.hypothesis_ids.append(str(hypothesis_id))
    return chain
