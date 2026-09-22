"""Attack Graph tenancy: Engagement isolation over one shared backend.

The Shared Attack Graph is intentionally shared *within* one Engagement —
across all its Runs, so a later run can build on an earlier run's
findings (``docs/contracts/attack-graph.md`` "Why IDs, not embedded
objects, in RuntimeState"). What must never happen is contamination
*between different Engagements* that end up sharing one underlying store
instance (e.g. a future persistent backend serving many engagements at
once). ``EngagementScopedAttackGraphStore`` is what makes that safe.
"""

from __future__ import annotations

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.base import StoreCapabilityError
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import EngagementId, GraphNodeId


def _store_for(
    engagement_id: str, shared: InMemoryAttackGraphStore
) -> EngagementScopedAttackGraphStore:
    return EngagementScopedAttackGraphStore(shared, engagement_id=EngagementId(engagement_id))


async def test_engagements_sharing_one_backend_do_not_see_each_others_nodes() -> None:
    shared = InMemoryAttackGraphStore()
    service_a = AttackGraphService(_store_for("eng_a", shared))
    service_b = AttackGraphService(_store_for("eng_b", shared))

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local")
    await service_b.upsert_node(GraphNodeType.HOST, key="b.local")

    nodes_a = await service_a.list_nodes()
    nodes_b = await service_b.list_nodes()

    assert {str(n.id) for n in nodes_a} == {"host:a.local"}
    assert {str(n.id) for n in nodes_b} == {"host:b.local"}


async def test_get_node_does_not_cross_engagement_boundaries() -> None:
    shared = InMemoryAttackGraphStore()
    service_a = AttackGraphService(_store_for("eng_a", shared))
    service_b = AttackGraphService(_store_for("eng_b", shared))

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local")

    assert await service_a.get_node(GraphNodeId("host:a.local")) is not None
    assert await service_b.get_node(GraphNodeId("host:a.local")) is None


async def test_same_node_id_in_two_engagements_are_independent() -> None:
    """Both engagements happen to observe "host:a.local" — they must not
    merge into one node or leak each other's properties."""
    shared = InMemoryAttackGraphStore()
    service_a = AttackGraphService(_store_for("eng_a", shared))
    service_b = AttackGraphService(_store_for("eng_b", shared))

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"owner": "a"})
    await service_b.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"owner": "b"})

    node_a = await service_a.get_node(GraphNodeId("host:a.local"))
    node_b = await service_b.get_node(GraphNodeId("host:a.local"))
    assert node_a is not None and node_b is not None
    assert node_a.properties["attrs"]["owner"] == "a"
    assert node_b.properties["attrs"]["owner"] == "b"


async def test_neighbors_and_edges_are_engagement_scoped() -> None:
    shared = InMemoryAttackGraphStore()
    service_a = AttackGraphService(_store_for("eng_a", shared))
    service_b = AttackGraphService(_store_for("eng_b", shared))

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local")
    await service_a.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    await service_a.link(
        GraphEdgeType.EXPOSES, GraphNodeId("host:a.local"), GraphNodeId("service:a.local/http")
    )

    assert len(await service_a.neighbors(GraphNodeId("host:a.local"))) == 1
    assert len(await service_a.list_edges()) == 1
    # eng_b never saw any of this.
    assert await service_b.list_edges() == []
    assert await service_b.neighbors(GraphNodeId("host:a.local")) == []


async def test_query_is_engagement_scoped() -> None:
    shared = InMemoryAttackGraphStore()
    service_a = AttackGraphService(_store_for("eng_a", shared))
    service_b = AttackGraphService(_store_for("eng_b", shared))

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local")
    await service_b.upsert_node(GraphNodeType.HOST, key="b.local")

    results = await service_a.query(node_type=GraphNodeType.HOST)
    assert {str(n.id) for n in results} == {"host:a.local"}


async def test_remove_node_only_affects_the_owning_engagement() -> None:
    shared = InMemoryAttackGraphStore()
    store_a = _store_for("eng_a", shared)
    store_b = _store_for("eng_b", shared)
    service_a = AttackGraphService(store_a)
    service_b = AttackGraphService(store_b)

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local")
    await service_b.upsert_node(GraphNodeType.HOST, key="a.local")

    assert await store_a.remove_node(GraphNodeId("host:a.local")) is True
    assert await service_a.get_node(GraphNodeId("host:a.local")) is None
    # eng_b's identically-keyed node survives.
    assert await service_b.get_node(GraphNodeId("host:a.local")) is not None


async def test_get_edge_does_not_cross_engagement_boundaries() -> None:
    shared = InMemoryAttackGraphStore()
    store_a = _store_for("eng_a", shared)
    service_a = AttackGraphService(store_a)
    service_b_store = _store_for("eng_b", shared)

    await service_a.upsert_node(GraphNodeType.HOST, key="a.local")
    await service_a.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    edge = await service_a.link(
        GraphEdgeType.EXPOSES, GraphNodeId("host:a.local"), GraphNodeId("service:a.local/http")
    )

    assert await store_a.get_edge(edge.id) is not None
    assert await service_b_store.get_edge(edge.id) is None


async def test_wrapper_rejects_an_engagement_id_containing_the_namespace_separator() -> None:
    with pytest.raises(ValueError, match="namespace separator"):
        EngagementScopedAttackGraphStore(
            InMemoryAttackGraphStore(), engagement_id=EngagementId("eng\x1fA")
        )


async def test_wrapper_still_requires_listable_mutable_capability_from_inner() -> None:
    class BareStore:
        async def add_node(self, node):  # pragma: no cover - not exercised
            raise NotImplementedError

        async def add_edge(self, edge):  # pragma: no cover - not exercised
            raise NotImplementedError

        async def get_node(self, node_id):
            return None

        async def get_edge(self, edge_id):
            return None

        async def neighbors(self, node_id):
            return []

        async def query(self, **filters):
            return []

    wrapper = EngagementScopedAttackGraphStore(BareStore(), engagement_id=EngagementId("eng_a"))
    with pytest.raises(StoreCapabilityError):
        await wrapper.list_nodes()
    with pytest.raises(StoreCapabilityError):
        await wrapper.remove_node(GraphNodeId("host:a.local"))


# -- intentional cross-run sharing within one Engagement ------------------


async def test_two_runs_of_the_same_engagement_share_the_graph() -> None:
    """Not a bug: RunId is not part of the isolation boundary. A later run
    of the same Engagement must see an earlier run's nodes — that is the
    entire point of a *Shared* Attack Graph outliving any single run. Only
    EngagementId isolates; see the module docstring."""
    shared = InMemoryAttackGraphStore()
    store = _store_for("eng_a", shared)
    run_1_service = AttackGraphService(store)
    run_2_service = AttackGraphService(store)  # a later run, same engagement/store

    await run_1_service.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True})

    node = await run_2_service.get_node(GraphNodeId("host:a.local"))
    assert node is not None
    assert node.properties["attrs"]["reachable"] is True
