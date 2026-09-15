"""Store capability beyond the frozen ``core`` port.

``AttackGraphStore`` (Contract v0.1) cannot enumerate nodes/edges, which
pattern resolution, observations, integrity checks and snapshots all
need. Declared here as an optional Agent-Team-owned Protocol instead of
changing the contract; see the Contract Change Proposal in
``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redteam.core.contracts.graph import GraphEdge, GraphNode


@runtime_checkable
class ListableAttackGraphStore(Protocol):
    """An ``AttackGraphStore`` that can also enumerate what it holds."""

    async def list_nodes(self) -> list[GraphNode]: ...

    async def list_edges(self) -> list[GraphEdge]: ...


class StoreCapabilityError(RuntimeError):
    """Raised when a store cannot support the requested graph operation."""

    def __init__(self, operation: str, store: object) -> None:
        super().__init__(
            f"{type(store).__name__} cannot support {operation}: it does not implement "
            f"redteam.attack_graph.stores.base.ListableAttackGraphStore "
            f"(list_nodes/list_edges)"
        )


def require_listable(store: object, operation: str) -> ListableAttackGraphStore:
    if not isinstance(store, ListableAttackGraphStore):
        raise StoreCapabilityError(operation, store)
    return store
