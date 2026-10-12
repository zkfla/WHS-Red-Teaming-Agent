"""Fifth hardening pass: issues that survived the fourth.

External re-review found each of these still reproducing after the
previous round of fixes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import (
    ATTRS_SEEN_KEY,
    SensitiveValueError,
    attr_age,
    attrs_of,
    build_properties,
    validate_properties,
)
from redteam.attack_graph.retraction import RetractionRule
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.traversal import walk
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from tests.fakes.graph_records import records_for, seed_records

NOW = datetime.now(timezone.utc)
SECRET = "FAKE_TEST_SECRET"


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


def node(node_id: str, node_type: GraphNodeType = GraphNodeType.HOST, **kwargs) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=build_properties(**kwargs),
        created_at=NOW,
        updated_at=NOW,
    )


# -- the attempt record is what decides whether to act --------------------


async def test_has_attempted_remembers_the_oldest_operation() -> None:
    svc = service()
    target = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    for index in range(201):
        await svc.record_attempt(target.id, f"op{index:03d}/x", step=index)

    assert await svc.has_attempted(target.id, "op000/x") is True
    assert await svc.has_attempted(target.id, "op200/x") is True


# -- dropping a bind can orphan another, and another ----------------------


def test_a_required_edge_constraint_cannot_be_orphaned_away() -> None:
    # A optional (no candidates) -> B optional and tied to A -> dropped ->
    # C is required and tied to B. A single cleanup pass left C's
    # constraint pointing at nothing, so it passed with no relation at all.
    nodes = [
        node("resource:middle", GraphNodeType.RESOURCE),
        node("resource:end", GraphNodeType.RESOURCE),
    ]
    patterns = [
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="a", optional=True),
        NodePattern(
            type=GraphNodeType.RESOURCE,
            bind="b",
            optional=True,
            related_to="a",
            edge_type=GraphEdgeType.OWNS,
        ),
        NodePattern(
            type=GraphNodeType.RESOURCE,
            bind="c",
            related_to="b",
            edge_type=GraphEdgeType.OWNS,
        ),
    ]

    result = resolve(patterns, nodes=nodes, edges=[])

    assert not result.ok
    assert "requires an edge to" in result.reason


# -- exclusion is pairwise, not transitive --------------------------------


def test_a_chain_of_exclusions_does_not_demand_one_node_per_bind() -> None:
    # A≠B and B≠C leave A=C legal, so two nodes suffice. Treating the whole
    # connected component as one group rejected an assignment that exists.
    nodes = [node("host:h1"), node("host:h2")]
    patterns = [
        NodePattern(type=GraphNodeType.HOST, bind="A", distinct_from=["B"]),
        NodePattern(type=GraphNodeType.HOST, bind="B", distinct_from=["A", "C"]),
        NodePattern(type=GraphNodeType.HOST, bind="C", distinct_from=["B"]),
    ]

    result = resolve(patterns, nodes=nodes)

    assert result.ok
    assert result.one("A") != result.one("B")
    assert result.one("B") != result.one("C")


def test_a_real_clique_is_still_counted_out_up_front() -> None:
    nodes = [node("host:h1"), node("host:h2")]
    patterns = [
        NodePattern(type=GraphNodeType.HOST, bind="A", distinct_from=["B", "C"]),
        NodePattern(type=GraphNodeType.HOST, bind="B", distinct_from=["A", "C"]),
        NodePattern(type=GraphNodeType.HOST, bind="C", distinct_from=["A", "B"]),
    ]
    result = resolve(patterns, nodes=nodes)
    assert not result.ok
    assert "must not overlap" in result.reason


# -- a predicate ages on its own, not on the node -------------------------


async def test_an_unrelated_write_does_not_renew_a_predicate() -> None:
    svc = service()
    created = await svc.upsert_node(
        GraphNodeType.CREDENTIAL, key="cred-ref", attrs={"valid": True}, step=1
    )
    await svc.record_attempt(created.id, "analysis/inspect", step=9)

    applied = await svc.sweep_stale_predicates(current_step=10)

    assert [item.attr for item in applied] == ["valid"]
    refreshed = await svc.get_node(created.id)
    assert refreshed is not None
    assert "valid" not in attrs_of(refreshed.properties)


async def test_an_unrelated_write_does_not_block_a_merged_retraction() -> None:
    source = service()
    created = await source.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await source.retract(created.id, "reachable", step=5, reason="host is down")
    records = await records_for(source)

    destination = service()
    await destination.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await destination.upsert_node(
        GraphNodeType.HOST, key="a.local", facts={"note": "unrelated"}, step=9
    )
    await seed_records(destination, records, merge=True)

    merged = await destination.get_node(created.id)
    assert merged is not None
    assert "reachable" not in attrs_of(merged.properties)


async def test_re_asserting_a_predicate_renews_only_that_one() -> None:
    svc = service()
    created = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True, "probed": True}, step=1
    )
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=20)

    refreshed = await svc.get_node(created.id)
    assert refreshed is not None
    assert attr_age(refreshed.properties, "reachable", 20) == 0
    assert attr_age(refreshed.properties, "probed", 20) == 19
    assert ATTRS_SEEN_KEY in refreshed.properties


async def test_a_pattern_ages_the_predicate_it_asks_about() -> None:
    svc = service()
    await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    node_id = GraphNodeId("host:a.local")
    await svc.record_attempt(node_id, "unrelated/op", step=30)

    pattern = [
        NodePattern(
            type=GraphNodeType.HOST, attrs={"reachable": True}, bind="h", max_age_steps=5
        )
    ]
    assert not (await svc.resolve(pattern, current_step=30)).ok


# -- the secret guard covers every write path -----------------------------


def test_validate_properties_scans_custom_top_level_keys() -> None:
    with pytest.raises(SensitiveValueError):
        validate_properties({"attrs": {}, "custom": {"password": SECRET}})


async def test_a_custom_property_key_cannot_smuggle_a_secret() -> None:
    svc = service()
    with pytest.raises(SensitiveValueError):
        await svc.upsert_node(
            GraphNodeType.HOST,
            {
                "attrs": {},
                "facts": {},
                "provenance": [],
                "retractions": [],
                "first_seen_step": 0,
                "last_seen_step": 0,
                "custom": {"password": SECRET},
            },
            node_id=GraphNodeId("host:a.local"),
        )
    assert SECRET not in str(await records_for(svc))


async def test_edge_properties_are_scanned_too() -> None:
    svc = service()
    left = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    right = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/http")

    with pytest.raises(SensitiveValueError):
        await svc.link(
            GraphEdgeType.HOSTS, left.id, right.id, properties={"metadata": {"password": SECRET}}
        )
    assert SECRET not in str(await records_for(svc))


async def test_importing_a_snapshot_is_not_a_way_around_the_guard() -> None:
    # Write it to a file, read it back would otherwise bypass every check.
    hostile: list[dict[str, object]] = [
        {"_collection": "graph_meta", "schema_version": 1},
        {
            "_collection": "nodes",
            "id": "host:x",
            "type": "host",
            "labels": [],
            "properties": {
                "attrs": {},
                "facts": {"password": SECRET},
                "provenance": [],
                "retractions": [],
                "first_seen_step": 0,
                "last_seen_step": 0,
            },
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        },
    ]
    svc = service()
    with pytest.raises(SensitiveValueError):
        await seed_records(svc, hostile)
    assert SECRET not in str(await records_for(svc))


# -- traversal limits and branch rendering --------------------------------


def test_walk_honours_max_nodes_within_a_single_hop() -> None:
    # The cap was checked per dequeue, so one node with a hundred
    # neighbours returned all hundred.
    edges = [
        GraphEdge(
            id=GraphEdgeId(f"e{index}"),
            type=GraphEdgeType.RELATED_TO,
            source_id=GraphNodeId("host:root"),
            target_id=GraphNodeId(f"host:leaf{index}"),
            created_at=NOW,
        )
        for index in range(100)
    ]
    assert len(walk(GraphNodeId("host:root"), edges=edges, max_nodes=5)) == 5






async def test_custom_retraction_rules_use_per_predicate_age() -> None:
    svc = service()
    created = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"probed": True}, step=1
    )
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", facts={"note": "x"}, step=30)

    applied = await svc.sweep_stale_predicates(
        current_step=30,
        rules=[RetractionRule(attr="probed", max_age_steps=5, reason="probe expired")],
    )
    assert [item.attr for item in applied] == ["probed"]
    assert "29" in applied[0].reason or "expired" in applied[0].reason
    assert created.id == GraphNodeId("host:a.local")
