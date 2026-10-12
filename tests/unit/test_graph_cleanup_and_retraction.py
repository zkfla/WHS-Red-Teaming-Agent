"""Cleanup obligations, stale-predicate sweeps, and the store's edge index."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.cleanup import CleanupStatus, collect
from redteam.attack_graph.predicates import attrs_of, build_properties, cleanup_of, merge_properties
from redteam.attack_graph.retraction import RetractionRule, plan_retractions
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId
from tests.fakes.graph_records import records_for, seed_records

NOW = datetime.now(timezone.utc)


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


# -- cleanup --------------------------------------------------------------


async def test_a_created_resource_is_tracked_until_it_is_cleaned() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.PRINCIPAL, key="test-user-1")

    item = await svc.register_created_resource(
        node.id, resource_type="user_account", action_id="action_1", step=2
    )
    assert item is not None and item.status is CleanupStatus.PENDING
    assert [i.node_id for i in await svc.pending_cleanup()] == [node.id]

    # A run holding a leftover must not be reportable.
    report = await svc.integrity_report()
    assert report.uncleaned_resources
    assert any("uncleaned resource" in line for line in report.blocking)

    await svc.mark_cleaned(node.id, evidence_id="ev_9")

    assert await svc.pending_cleanup() == []
    assert (await svc.cleanup_report()).is_clear
    assert (await svc.integrity_report()).uncleaned_resources == []


async def test_claiming_cleanup_without_evidence_is_refused() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.ASSET, key="tmp-file")
    await svc.register_created_resource(node.id, resource_type="file")

    with pytest.raises(ValueError, match="evidence"):
        await svc.mark_cleaned(node.id, evidence_id="")


async def test_a_failed_cleanup_stays_outstanding() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.ASSET, key="tmp-file")
    await svc.register_created_resource(node.id, resource_type="file")

    item = await svc.mark_cleanup_failed(node.id, note="delete returned 403")
    assert item is not None and item.status is CleanupStatus.FAILED
    assert item.is_outstanding
    assert len(await svc.pending_cleanup()) == 1


async def test_not_required_clears_the_obligation() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.ASSET, key="read-only")
    await svc.register_created_resource(node.id, resource_type="file")

    await svc.mark_cleanup_not_required(node.id, note="nothing was actually written")
    assert (await svc.cleanup_report()).is_clear


async def test_cleanup_calls_on_unknown_nodes_are_no_ops() -> None:
    svc = service()
    missing = GraphNodeId("asset:nope")
    assert await svc.register_created_resource(missing, resource_type="file") is None
    assert await svc.mark_cleanup_failed(missing, note="x") is None


async def test_cleanup_survives_a_snapshot_round_trip() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.PRINCIPAL, key="test-user-1")
    await svc.register_created_resource(node.id, resource_type="user_account", step=1)

    restored = service()
    await seed_records(restored, await records_for(svc))

    assert [i.node_id for i in await restored.pending_cleanup()] == [node.id]


def test_a_later_observation_cannot_reset_a_cleaned_resource() -> None:
    cleaned = build_properties(cleanup={"resource_type": "file", "status": "cleaned"})
    stale = build_properties(cleanup={"resource_type": "file", "status": "pending"})

    merged = cleanup_of(merge_properties(cleaned, stale))
    assert merged is not None and merged["status"] == "cleaned"


def test_collect_reads_items_off_the_nodes() -> None:
    node = GraphNode(
        id=GraphNodeId("asset:x"),
        type=GraphNodeType.ASSET,
        properties=build_properties(cleanup={"resource_type": "file", "status": "pending"}),
        created_at=NOW,
        updated_at=NOW,
    )
    report = collect([node])
    assert len(report.outstanding) == 1
    assert "outstanding" in report.describe()


# -- retraction sweep -----------------------------------------------------


async def test_a_stale_predicate_is_withdrawn_with_a_reason() -> None:
    svc = service()
    node = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True, "in_scope": True}, step=1
    )

    applied = await svc.sweep_stale_predicates(current_step=20)

    assert [item.attr for item in applied] == ["reachable"]
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    # Only the stale predicate goes; scope membership is not an observation.
    assert attrs_of(refreshed.properties) == {"in_scope": True}
    assert refreshed.properties["retractions"][0]["key"] == "reachable"
    assert "re-confirmed" in refreshed.properties["retractions"][0]["reason"]


async def test_a_fresh_predicate_is_left_alone() -> None:
    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1)
    assert await svc.sweep_stale_predicates(current_step=3) == []


async def test_dry_run_reports_without_writing() -> None:
    svc = service()
    node = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )

    planned = await svc.sweep_stale_predicates(current_step=30, dry_run=True)

    assert len(planned) == 1
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None and attrs_of(refreshed.properties) == {"reachable": True}


async def test_custom_rules_replace_the_defaults() -> None:
    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"probed": True}, step=1)

    assert await svc.sweep_stale_predicates(current_step=30) == []
    applied = await svc.sweep_stale_predicates(
        current_step=30,
        rules=[RetractionRule(attr="probed", max_age_steps=2, reason="probe result expired")],
    )
    assert [item.attr for item in applied] == ["probed"]


def test_rules_are_scoped_to_a_node_type() -> None:
    node = GraphNode(
        id=GraphNodeId("endpoint:a GET /x"),
        type=GraphNodeType.ENDPOINT,
        properties=build_properties(attrs={"reachable": True}, step=0),
        created_at=NOW,
        updated_at=NOW,
    )
    host_only = [
        RetractionRule(attr="reachable", max_age_steps=1, node_type=GraphNodeType.HOST)
    ]
    assert plan_retractions([node], current_step=50, rules=host_only) == []
    assert len(plan_retractions([node], current_step=50)) == 1


async def test_a_retracted_predicate_removes_the_step_from_the_frontier() -> None:
    # The whole point: a precondition that stopped holding stops being
    # offered, instead of the planner failing against it every turn.
    from redteam.attack_graph.patterns import NodePattern, resolve

    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1)
    pattern = [NodePattern(type=GraphNodeType.HOST, attrs={"reachable": True}, bind="h")]

    assert resolve(pattern, nodes=await svc.list_nodes()).ok

    await svc.sweep_stale_predicates(current_step=20)
    assert not resolve(pattern, nodes=await svc.list_nodes()).ok


# -- store edge index -----------------------------------------------------


async def test_neighbors_use_the_index_and_stay_correct_after_re_linking() -> None:
    store = InMemoryAttackGraphStore()
    svc = AttackGraphService(store)
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    service_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    other = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/ssh")

    await svc.link(GraphEdgeType.HOSTS, host.id, service_node.id)
    await svc.link(GraphEdgeType.HOSTS, host.id, other.id)
    await svc.link(GraphEdgeType.HOSTS, host.id, service_node.id)  # re-link, same edge

    assert {n.id for n in await store.neighbors(host.id)} == {service_node.id, other.id}
    assert len(await store.list_edges()) == 2
    assert len(await store.incident_edges(host.id, direction="out")) == 2
    assert await store.incident_edges(host.id, direction="in") == []
    assert len(await store.incident_edges(service_node.id, direction="in")) == 1
