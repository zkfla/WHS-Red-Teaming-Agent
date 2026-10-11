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

from redteam.attack_graph.stores.base import (
    StoreCapabilityError,
    require_listable,
    require_mutable,
    require_receipted,
)
from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationConflict,
    GraphMutationReceipt,
    checked_receipt,
)
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

    async def prepare_graph_mutation(self, mutation: GraphMutation) -> GraphMutation:
        require_receipted(self._inner)
        mutation = GraphMutation.model_validate(mutation.model_dump()).model_copy(deep=True)
        if mutation.namespace:
            raise GraphMutationConflict("graph_mutation_namespace_already_prepared")
        changes: dict[str, Any] = {"namespace": self._ns}
        if mutation.node is not None:
            changes["node"] = mutation.node.model_copy(update={
                "id": self._namespaced(mutation.node.id),
            })
        elif mutation.edge is not None:
            changes["edge"] = mutation.edge.model_copy(update={
                "id": self._namespaced_edge(mutation.edge.id),
                "source_id": self._namespaced(mutation.edge.source_id),
                "target_id": self._namespaced(mutation.edge.target_id),
            })
        elif mutation.operation == "remove_node":
            assert mutation.identity is not None
            changes["identity"] = str(self._namespaced(GraphNodeId(mutation.identity)))
        else:
            assert mutation.identity is not None
            # Legacy removal may involve two physical IDs. Do not silently
            # change that behavior or pretend a one-ID receipt proves both.
            legacy = await self._inner.get_edge(GraphEdgeId(mutation.identity))
            if (legacy is not None and self._owns_edge(legacy)
                    and _NS_SEPARATOR not in str(legacy.id)):
                raise StoreCapabilityError(
                    "legacy_edge_removal_receipt", self._inner, "ReceiptedAttackGraphStore",
                )
            changes["identity"] = str(self._namespaced_edge(GraphEdgeId(mutation.identity)))
        return mutation.model_copy(update=changes, deep=True)

    async def apply_graph_mutation(self, mutation: GraphMutation) -> GraphMutationReceipt:
        mutation = GraphMutation.model_validate(mutation.model_dump()).model_copy(deep=True)
        owns = (mutation.namespace == self._ns)
        if mutation.node is not None:
            owns = owns and self._owns(str(mutation.node.id))
        elif mutation.edge is not None:
            owns = (owns and self._owns_edge_id(str(mutation.edge.id))
                    and self._owns_edge(mutation.edge))
        else:
            owns = owns and self._owns(str(mutation.identity))
        if not owns:
            raise GraphMutationConflict("graph_mutation_namespace_conflict")
        receipt = await require_receipted(self._inner).apply_graph_mutation(mutation)
        return checked_receipt(receipt, mutation)

    async def read_graph_mutation_receipt(
        self, run_id: str, mutation_id: str, *, namespace: str = "",
    ) -> GraphMutationReceipt | None:
        if namespace:
            raise GraphMutationConflict("graph_mutation_namespace_already_prepared")
        receipt = await require_receipted(self._inner).read_graph_mutation_receipt(
            run_id, mutation_id, namespace=self._ns,
        )
        if receipt is not None and (
            receipt.namespace != self._ns or receipt.run_id != run_id
            or receipt.mutation_id != mutation_id
        ):
            raise GraphMutationConflict("graph_mutation_receipt_index_conflict")
        return receipt

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
