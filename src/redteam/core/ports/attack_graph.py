"""Shared Attack Graph store port. Concrete stores in ``redteam.attack_graph.stores``.

Initial implementation is ``InMemoryAttackGraphStore``. A graph-DB backed
store (Neo4j etc.) can be added later behind this same Protocol —
explicitly out of scope for this stage. ``query`` is deliberately not a
DSL — just keyword filters — to avoid committing to a query language
before a real backend is chosen.
"""

from __future__ import annotations

from typing import Any, Protocol

from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.ids import GraphEdgeId, GraphNodeId


class AttackGraphStore(Protocol):
    async def add_node(self, node: GraphNode) -> None: ...

    async def add_edge(self, edge: GraphEdge) -> None: ...

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None: ...

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None: ...

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]: ...

    async def query(self, **filters: Any) -> list[GraphNode]: ...
