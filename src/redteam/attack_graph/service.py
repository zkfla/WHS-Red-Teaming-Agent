"""Attack Graph convenience API. Owned by Agent Team.

Orchestration nodes and agents should go through this service rather
than poking a store directly — it's the seam where Agent Team can add
richer graph-building logic (dedup, merge-by-identity, Hypothesis ->
Finding promotion, ...) without every caller needing to know about it.

TODO(agent-team): flesh out hypothesis/finding lifecycle helpers,
node dedup/merge-by-identity, and richer ``query`` filters.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId, new_id
from redteam.core.ports.attack_graph import AttackGraphStore


class AttackGraphService:
    def __init__(self, store: AttackGraphStore) -> None:
        self._store = store

    async def upsert_node(
        self,
        node_type: GraphNodeType,
        properties: dict[str, Any],
        node_id: GraphNodeId | None = None,
    ) -> GraphNode:
        now = datetime.now(timezone.utc)
        existing = await self._store.get_node(node_id) if node_id else None
        node = GraphNode(
            id=node_id or GraphNodeId(new_id("node")),
            type=node_type,
            properties=properties,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        await self._store.add_node(node)
        return node

    async def link(
        self,
        edge_type: GraphEdgeType,
        source_id: GraphNodeId,
        target_id: GraphNodeId,
        properties: dict[str, Any] | None = None,
    ) -> GraphEdge:
        edge = GraphEdge(
            id=GraphEdgeId(new_id("edge")),
            type=edge_type,
            source_id=source_id,
            target_id=target_id,
            properties=properties or {},
            created_at=datetime.now(timezone.utc),
        )
        await self._store.add_edge(edge)
        return edge

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return await self._store.get_node(node_id)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        return await self._store.neighbors(node_id)
