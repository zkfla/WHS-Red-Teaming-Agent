"""Engagement-scoped ``AttackGraphStore`` wrapper.

The Shared Attack Graph is deliberately shared *within* one Engagement —
across all its Runs (``docs/contracts/attack-graph.md`` "Why IDs, not
embedded objects, in RuntimeState") — but must never leak between
*different* Engagements if they end up sharing one underlying store
instance (a future persistent/shared backend, or a caller that reuses one
store across multiple ``build_application``/``build_runtime_deps`` calls
by mistake). Today's default wiring (a fresh ``InMemoryAttackGraphStore``
per call) already avoids this by construction, so this wrapper is what
makes that safe under a *shared* backend too, rather than something every
caller must remember to get right.

Design: translate every id at the store I/O boundary only. Callers
(``AttackGraphService`` and everything built on it) keep working with
plain, un-namespaced ``GraphNodeId``/``GraphEdgeId`` values exactly as
before — ``node_id_for_target``, ``split_node_id`` and friends never see
a namespaced id. The namespace prefix exists only as this wrapper's own
key into the underlying store, using a separator (``\\x1f``, ASCII Unit
Separator) that ``attack_graph.identity.make_node_id`` already rejects
inside a key — so it can never collide with a real id segment.

No ``core`` port signature changes: this implements the exact same
``AttackGraphStore`` Protocol (plus the optional ``Listable``/``Mutable``
Protocols ``InMemoryAttackGraphStore`` already supports), so it is a
drop-in substitute anywhere a store is wired today.
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.stores.base import require_listable, require_mutable
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.ids import EngagementId, GraphEdgeId, GraphNodeId
from redteam.core.ports.attack_graph import AttackGraphStore

_NS_SEPARATOR = "\x1f"


class EngagementScopedAttackGraphStore:
    """Namespaces every id written to/read from ``inner`` by ``engagement_id``.

    Construct one per Engagement (explicitly, via the constructor
    argument — never inferred) and hand it to that Engagement's
    ``RuntimeDeps``/``AttackGraphService`` in place of ``inner`` directly.
    """

    def __init__(self, inner: AttackGraphStore, *, engagement_id: EngagementId) -> None:
        self._inner = inner
        self._ns = str(engagement_id)
        if _NS_SEPARATOR in self._ns:
            raise ValueError("engagement_id must not contain the namespace separator")

    # -- id translation -----------------------------------------------

    def _namespaced(self, node_id: GraphNodeId) -> GraphNodeId:
        return GraphNodeId(f"{self._ns}{_NS_SEPARATOR}{node_id}")

    def _stripped(self, namespaced_id: GraphNodeId) -> GraphNodeId:
        prefix = f"{self._ns}{_NS_SEPARATOR}"
        raw = str(namespaced_id)
        if not raw.startswith(prefix):
            raise ValueError(
                f"id {namespaced_id!r} does not belong to engagement namespace {self._ns!r}"
            )
        return GraphNodeId(raw[len(prefix) :])

    def _owns(self, namespaced_id: str) -> bool:
        return namespaced_id.startswith(f"{self._ns}{_NS_SEPARATOR}")

    def _namespaced_edge(self, edge_id: GraphEdgeId) -> GraphEdgeId:
        return GraphEdgeId(f"{self._ns}{_NS_SEPARATOR}{edge_id}")

    def _stripped_edge(self, namespaced_id: GraphEdgeId) -> GraphEdgeId:
        prefix = f"{self._ns}{_NS_SEPARATOR}"
        raw = str(namespaced_id)
        if not raw.startswith(prefix):
            raise ValueError(
                f"edge id {namespaced_id!r} does not belong to engagement namespace {self._ns!r}"
            )
        return GraphEdgeId(raw[len(prefix) :])

    def _owns_edge_id(self, edge_id: str) -> bool:
        return edge_id.startswith(f"{self._ns}{_NS_SEPARATOR}")

    def _owns_edge(self, edge: GraphEdge) -> bool:
        return self._owns(str(edge.source_id)) and self._owns(str(edge.target_id))

    def _translate_node(self, node: GraphNode) -> GraphNode:
        return node.model_copy(update={"id": self._stripped(node.id)})

    def _translate_edge(self, edge: GraphEdge) -> GraphEdge:
        if self._owns_edge_id(str(edge.id)):
            plain_id = self._stripped_edge(edge.id)
        else:
            plain_id = edge.id
        return edge.model_copy(
            update={
                "id": plain_id,
                "source_id": self._stripped(edge.source_id),
                "target_id": self._stripped(edge.target_id),
            }
        )

    # -- AttackGraphStore (core port) ----------------------------------

    async def add_node(self, node: GraphNode) -> None:
        await self._inner.add_node(node.model_copy(update={"id": self._namespaced(node.id)}))

    async def add_edge(self, edge: GraphEdge) -> None:
        await self._inner.add_edge(
            edge.model_copy(
                update={
                    "id": self._namespaced_edge(edge.id),
                    "source_id": self._namespaced(edge.source_id),
                    "target_id": self._namespaced(edge.target_id),
                }
            )
        )

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        node = await self._inner.get_node(self._namespaced(node_id))
        return None if node is None else self._translate_node(node)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        namespaced_id = self._namespaced_edge(edge_id)
        edge = await self._inner.get_edge(namespaced_id)
        if edge is not None and self._owns_edge(edge):
            return self._translate_edge(edge)
        legacy = await self._inner.get_edge(edge_id)
        if legacy is None or not self._owns_edge(legacy):
            return None
        if _NS_SEPARATOR in str(legacy.id):
            return None
        return self._translate_edge(legacy)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        nodes = await self._inner.neighbors(self._namespaced(node_id))
        return [self._translate_node(n) for n in nodes if self._owns(str(n.id))]

    async def query(self, **filters: Any) -> list[GraphNode]:
        nodes = await self._inner.query(**filters)
        return [self._translate_node(n) for n in nodes if self._owns(str(n.id))]

    # -- ListableAttackGraphStore (optional) ---------------------------

    async def list_nodes(self) -> list[GraphNode]:
        listable = require_listable(self._inner, "list_nodes")
        nodes = await listable.list_nodes()
        return [self._translate_node(n) for n in nodes if self._owns(str(n.id))]

    async def list_edges(self) -> list[GraphEdge]:
        listable = require_listable(self._inner, "list_edges")
        edges = await listable.list_edges()
        out = {}
        for e in edges:
            if self._owns_edge(e) and (
                self._owns_edge_id(str(e.id)) or _NS_SEPARATOR not in str(e.id)
            ):
                translated = self._translate_edge(e)
                if translated.id not in out or self._owns_edge_id(str(e.id)):
                    out[translated.id] = translated
        return list(out.values())

    # -- MutableAttackGraphStore (optional) ----------------------------

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        mutable = require_mutable(self._inner, "remove_node")
        return await mutable.remove_node(self._namespaced(node_id))

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        mutable = require_mutable(self._inner, "remove_edge")
        namespaced_id = self._namespaced_edge(edge_id)
        edge = await self._inner.get_edge(namespaced_id)
        removed = False
        if edge is not None and self._owns_edge(edge):
            removed = await mutable.remove_edge(namespaced_id)
        legacy = await self._inner.get_edge(edge_id)
        if legacy is not None and self._owns_edge(legacy) and _NS_SEPARATOR not in str(legacy.id):
            removed = await mutable.remove_edge(edge_id) or removed
        return removed
