"""Store capabilities beyond the frozen ``core`` port.

``AttackGraphStore`` (Contract v0.1) cannot enumerate or remove. Declared
here as optional Protocols rather than changing the contract — see the
Contract Change Proposal in ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


class StoreCapabilityError(RuntimeError):
    """Raised when a store cannot support the requested graph operation."""

    def __init__(self, operation: str, store: object, protocol: str) -> None:
        super().__init__(
            f"{type(store).__name__} cannot support {operation}: it does not implement "
            f"redteam.attack_graph.stores.base.{protocol}"
        )


def require_listable(store: object, operation: str) -> ListableAttackGraphStore:
    if not isinstance(store, ListableAttackGraphStore):
        raise StoreCapabilityError(operation, store, "ListableAttackGraphStore")
    return store


def require_mutable(store: object, operation: str) -> MutableAttackGraphStore:
    if not isinstance(store, MutableAttackGraphStore):
        raise StoreCapabilityError(operation, store, "MutableAttackGraphStore")
    return store
