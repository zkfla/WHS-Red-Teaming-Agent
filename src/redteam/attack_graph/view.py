"""Read-only handle onto the Attack Graph.

Given to components that must not write; writes go through
``AttackGraphService``.
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.cleanup import CleanupReport, collect
from redteam.attack_graph.integrity import IntegrityReport, check_integrity
from redteam.attack_graph.observation import (
    ATTEMPTED_FACT_KEY,
    DEFAULT_MAX_ATTEMPTED,
    DEFAULT_MAX_PER_TYPE,
    DEFAULT_MAX_TOTAL,
    GraphObservation,
    build_observation,
)
from redteam.attack_graph.patterns import NodePattern, ResolveResult, resolve
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.stores.base import require_listable
from redteam.attack_graph.traversal import (
    Direction,
    subgraph_around,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.core.ports.attack_graph import AttackGraphStore

_READ_METHODS = frozenset(
    {
        "get_node",
        "cacheable",
        "get_edge",
        "incident_edges",
        "neighbors",
        "query",
        "list_nodes",
        "list_edges",
        "has_edge",
        "has_attempted",
        "observe",
        "resolve",
        "integrity_report",
        "counts",
        "subgraph_around",
        "cleanup_report",
    }
)


class AttackGraphView:
    """Query-only façade. No ``add_*``/``upsert``/``record_*``."""

    __slots__ = ("_store",)

    def __init__(self, store: AttackGraphStore) -> None:
        self._store = store

    @property
    def cacheable(self) -> bool:
        """Whether caller-managed revisions can safely cache this backend's reads."""
        return bool(getattr(self._store, "cacheable", True))

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return await self._store.get_node(node_id)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        return await self._store.get_edge(edge_id)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        return await self._store.neighbors(node_id)

    async def query(self, **filters: Any) -> list[GraphNode]:
        return await self._store.query(**filters)

    async def list_nodes(self) -> list[GraphNode]:
        return await require_listable(self._store, "list_nodes").list_nodes()

    async def list_edges(self) -> list[GraphEdge]:
        return await require_listable(self._store, "list_edges").list_edges()

    async def incident_edges(
        self, node_id: GraphNodeId, *, direction: Direction = "any",
    ) -> list[GraphEdge]:
        method = getattr(self._store, "incident_edges", None)
        if callable(method):
            return await method(node_id, direction=direction)
        return [edge for edge in await self.list_edges() if
                (direction in ("out", "any") and edge.source_id == node_id) or
                (direction in ("in", "any") and edge.target_id == node_id)]

    async def has_attempted(self, node_id: GraphNodeId, operation_key: str) -> bool:
        """Was ``operation_key`` already tried against this node?

        Authoritative, unlike ``GraphObservation.attempted``, which is
        capped for prompt size — deciding *whether to act* from the capped
        list re-runs whatever fell off the end.
        """
        node = await self._store.get_node(node_id)
        if node is None:
            return False
        return operation_key in (facts_of(node.properties).get(ATTEMPTED_FACT_KEY) or [])

    async def has_edge(
        self, source_id: GraphNodeId, target_id: GraphNodeId, edge_type: GraphEdgeType
    ) -> bool:
        return any(
            edge.source_id == source_id and edge.target_id == target_id and edge.type == edge_type
            for edge in await self.incident_edges(source_id, direction="out")
        )

    async def observe(
        self,
        *,
        step: int = 0,
        max_per_type: int = DEFAULT_MAX_PER_TYPE,
        max_total: int = DEFAULT_MAX_TOTAL,
        max_attempted: int = DEFAULT_MAX_ATTEMPTED,
        extra_content_keys: tuple[str, ...] = (),
    ) -> GraphObservation:
        return build_observation(
            await self.list_nodes(),
            edges=await self.list_edges(),
            step=step,
            max_per_type=max_per_type,
            max_total=max_total,
            max_attempted=max_attempted,
            extra_content_keys=extra_content_keys,
        )

    async def resolve(
        self,
        patterns: list[NodePattern],
        *,
        current_step: int = 0,
        params: dict[str, Any] | None = None,
        for_frontier: bool = False,
    ) -> ResolveResult:
        return resolve(
            patterns,
            nodes=await self.list_nodes(),
            edges=await self.list_edges(),
            current_step=current_step,
            params=params,
            for_frontier=for_frontier,
        )


    async def subgraph_around(
        self, node_id: GraphNodeId, *, radius: int = 1, direction: Direction = "any"
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        method = getattr(self._store, "subgraph_around", None)
        if callable(method):
            return await method(node_id, radius=radius, direction=direction)
        return subgraph_around(
            node_id,
            nodes=await self.list_nodes(),
            edges=await self.list_edges(),
            radius=radius,
            direction=direction,
        )


    async def cleanup_report(self) -> CleanupReport:
        return collect(await self.list_nodes())

    async def integrity_report(self) -> IntegrityReport:
        return check_integrity(await self.list_nodes(), await self.list_edges())

    async def counts(self) -> dict[str, int]:
        return {
            "nodes": len(await self.list_nodes()),
            "edges": len(await self.list_edges()),
        }

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"AttackGraphView has no {name!r} — it is read-only. Writes go through "
            f"redteam.attack_graph.AttackGraphService so identity normalization, "
            f"merge semantics and the secret guard always run "
            f"(readable members: {sorted(_READ_METHODS)})"
        )
