"""Store capabilities beyond the frozen ``core`` port.

``AttackGraphStore`` (Contract v0.1) cannot enumerate or remove. Declared
here as optional Protocols rather than changing the contract — see the
Contract Change Proposal in ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast, runtime_checkable

from redteam.attack_graph.stores.mutations import ReceiptedAttackGraphStore
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.ids import GraphEdgeId, GraphNodeId


@runtime_checkable
class ListableAttackGraphStore(Protocol):
    """An ``AttackGraphStore`` that can also enumerate what it holds."""

    async def list_nodes(self) -> list[GraphNode]: ...

    async def list_edges(self) -> list[GraphEdge]: ...


@runtime_checkable
class MutableAttackGraphStore(Protocol):
    """An ``AttackGraphStore`` that can also drop what it holds. For
    correcting a mistaken write — "no longer true" is a retraction."""

    async def remove_node(self, node_id: GraphNodeId) -> bool: ...

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool: ...


class AtomicAttackGraphStore(Protocol):
    """Apply a pure synchronous update to the latest row under one write lock.

    Returning None leaves a missing row absent; it does not remove a row.
    Callbacks must not perform external I/O and can run again on lock contention.
    """

    async def mutate_node(
        self, node_id: GraphNodeId,
        update: Callable[[GraphNode | None], GraphNode | None],
    ) -> GraphNode | None: ...

    async def mutate_edge(
        self, edge_id: GraphEdgeId,
        update: Callable[[GraphEdge | None], GraphEdge | None],
    ) -> GraphEdge | None: ...


class StoreCapabilityError(RuntimeError):
    """Raised when a store cannot support the requested graph operation."""

    def __init__(self, operation: str, store: object, protocol: str) -> None:
        super().__init__(
            f"{type(store).__name__} cannot support {operation}: it does not implement "
            f"redteam.attack_graph.stores.base.{protocol}"
        )


def require_listable(store: object, operation: str) -> ListableAttackGraphStore:
    if not _has_methods(store, "list_nodes", "list_edges"):
        raise StoreCapabilityError(operation, store, "ListableAttackGraphStore")
    return cast(ListableAttackGraphStore, store)


def require_mutable(store: object, operation: str) -> MutableAttackGraphStore:
    if not _has_methods(store, "remove_node", "remove_edge"):
        raise StoreCapabilityError(operation, store, "MutableAttackGraphStore")
    return cast(MutableAttackGraphStore, store)


def require_receipted(store: object) -> ReceiptedAttackGraphStore:
    if not _has_methods(
        store, "prepare_graph_mutation", "apply_graph_mutation", "read_graph_mutation_receipt",
    ):
        raise StoreCapabilityError("atomic_graph_mutation", store, "ReceiptedAttackGraphStore")
    return cast(ReceiptedAttackGraphStore, store)


def _has_methods(store: object, *names: str) -> bool:
    # Python 3.12+ runtime Protocol checks use static lookup and cannot see
    # a wrapper's deliberate __getattr__ delegation. Check the callable
    # owning port methods instead, without unwrapping or bypassing fences.
    # Missing/non-callable members still fail; this proves no permissions,
    # original receipt validity or successful external write.
    return all(callable(getattr(store, name, None)) for name in names)
