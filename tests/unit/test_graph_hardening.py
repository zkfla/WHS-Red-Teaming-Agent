"""Regression tests for the first hardening pass."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import endpoint_node_id, node_id_for_target, target_for_node
from redteam.attack_graph.patterns import (
    MAX_SEARCH_ASSIGNMENTS,
    NodePattern,
    resolve,
)
from redteam.attack_graph.predicates import SensitiveValueError, build_properties
from redteam.attack_graph.service import (
    AttackGraphService,
    NodeTypeConflictError,
)
from redteam.attack_graph.stores.base import (
    ListableAttackGraphStore,
    MutableAttackGraphStore,
)
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.target import EndpointTarget
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId
from tests.fakes.graph_records import records_for, seed_records

NOW = datetime.now(timezone.utc)


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


def hosts(count: int) -> list[GraphNode]:
    return [
        GraphNode(
            id=GraphNodeId(f"host:h{index}"),
            type=GraphNodeType.HOST,
            properties=build_properties(),
            created_at=NOW,
            updated_at=NOW,
        )
        for index in range(count)
    ]


def exclusive_patterns(binds: int, count: int = 3) -> list[NodePattern]:
    return [
        NodePattern(
            type=GraphNodeType.HOST,
            bind=f"b{index}",
            count=count,
            distinct_from=[f"b{other}" for other in range(binds) if other != index],
        )
        for index in range(binds)
    ]


# -- P1: resolve used to hang ---------------------------------------------


@pytest.mark.parametrize("binds", [4, 5, 8, 12])
def test_impossible_exclusion_fails_immediately_instead_of_hanging(binds: int) -> None:
    # Was exponential: binds=4 took 7.6s, binds=5 never finished. The
    # frontier is recomputed every turn, so that is a hang.
    patterns = exclusive_patterns(binds)
    started = time.perf_counter()

    result = resolve(patterns, nodes=hosts(binds * 3 - 1))

    assert time.perf_counter() - started < 1.0
    assert not result.ok
    assert "must not overlap and need" in result.reason


def test_a_satisfiable_exclusion_still_resolves() -> None:
    result = resolve(exclusive_patterns(4), nodes=hosts(12))
    assert result.ok
    assigned = [node_id for ids in result.bindings.values() for node_id in ids]
    assert len(set(assigned)) == 12  # all distinct


def test_an_undecidable_search_says_so_rather_than_claiming_no_match() -> None:
    # "we gave up" and "there is no match" are different answers; a caller
    # that conflates them silently drops a viable step.
    nodes = hosts(30) + [
        GraphNode(
            id=GraphNodeId("principal:p"),
            type=GraphNodeType.PRINCIPAL,
            properties=build_properties(),
            created_at=NOW,
            updated_at=NOW,
        )
    ]
    patterns: list[NodePattern] = [NodePattern(type=GraphNodeType.PRINCIPAL, bind="p")]
    patterns += [
        NodePattern(
            type=GraphNodeType.HOST,
            bind=f"b{index}",
            count=2,
            related_to="p",
            edge_type=GraphEdgeType.CAN_ACCESS,
        )
        for index in range(8)
    ]
    started = time.perf_counter()

    result = resolve(patterns, nodes=nodes, edges=[])

    assert time.perf_counter() - started < 5.0
    assert not result.ok
    assert MAX_SEARCH_ASSIGNMENTS > 0


# -- P1: the observation is supposed to fit in a prompt -------------------


async def test_the_attempt_list_is_capped() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    for index in range(500):
        await svc.record_attempt(node.id, f"cap{index}/op")

    observation = await svc.observe(step=1, max_attempted=25)

    assert len(observation.attempted) == 25
    assert observation.truncated is True


async def test_recent_nodes_win_the_attempt_budget() -> None:
    svc = service()
    old = await svc.upsert_node(GraphNodeType.HOST, key="old.local", step=1)
    new = await svc.upsert_node(GraphNodeType.HOST, key="new.local", step=90)
    await svc.record_attempt(old.id, "cap/old", step=1)
    await svc.record_attempt(new.id, "cap/new", step=90)

    observation = await svc.observe(step=91, max_attempted=1)

    assert observation.attempted == ["cap/new@host:new.local"]


# -- P2: the secret guard had three holes ---------------------------------


@pytest.mark.parametrize(
    "facts",
    [
        {"card": 4242424242424242},  # int, not str
        {"admin_password": "hunter2"},  # prefixed
        {"db_pwd": "x"},  # suffixed short form
        {"user.token": "x"},  # dotted
        {"API_KEY_V2": "x"},  # upper-cased and suffixed
    ],
)
def test_secret_shaped_values_are_rejected(facts: dict) -> None:
    with pytest.raises(SensitiveValueError):
        build_properties(facts=facts)


def test_the_extra_path_no_longer_bypasses_the_guard() -> None:
    with pytest.raises(SensitiveValueError):
        build_properties(extra={"password": "hunter2"})


@pytest.mark.parametrize(
    "facts",
    [
        {"spinning": "x"},  # contains "pin"
        {"mapping": "x"},  # contains "pin"
        {"port": 8443},  # short int
        {"sort_key": "name"},  # contains "key" but is not a secret
    ],
)
def test_ordinary_values_still_pass(facts: dict) -> None:
    assert build_properties(facts=facts)


def test_true_is_not_mistaken_for_a_pan() -> None:
    assert build_properties(attrs={"reachable": True})


# -- P2: endpoints on different ports are different nodes -----------------


def test_the_port_is_part_of_an_endpoint_identity() -> None:
    plain = node_id_for_target(EndpointTarget(host="a.local", port=80, path="/x"))
    tls = node_id_for_target(EndpointTarget(host="a.local", port=8443, path="/x"))
    assert plain != tls
    assert str(tls) == "endpoint:a.local:8443 ANY /x"


def test_an_endpoint_with_a_port_round_trips() -> None:
    target = EndpointTarget(host="a.local", port=8443, path="/orders/12")
    back = target_for_node(node_id_for_target(target), GraphNodeType.ENDPOINT)
    assert isinstance(back, EndpointTarget)
    assert (back.host, back.port, back.path) == ("a.local", 8443, "/orders/{id}")


def test_an_endpoint_without_a_port_still_round_trips() -> None:
    back = target_for_node(
        endpoint_node_id("GET", "/x", host="a.local"), GraphNodeType.ENDPOINT
    )
    assert isinstance(back, EndpointTarget)
    assert (back.host, back.port) == ("a.local", None)


# -- P2: a type mismatch used to be swallowed -----------------------------


async def test_upserting_an_existing_node_as_another_type_is_refused() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")

    with pytest.raises(NodeTypeConflictError, match="refusing"):
        await svc.upsert_node(GraphNodeType.SERVICE, node_id=node.id)

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None and refreshed.type is GraphNodeType.HOST


# -- P3: snapshot import semantics are now explicit -----------------------


async def test_import_replaces_by_default_and_merges_on_request() -> None:
    source = service()
    await source.upsert_node(GraphNodeType.HOST, key="a", attrs={"x": True}, step=5)
    records = await records_for(source)

    replaced = service()
    await replaced.upsert_node(GraphNodeType.HOST, key="a", attrs={"y": True}, step=9)
    await seed_records(replaced, records)
    restored = await replaced.get_node(GraphNodeId("host:a"))
    assert restored is not None
    assert "y" not in restored.properties["attrs"]

    merged = service()
    await merged.upsert_node(GraphNodeType.HOST, key="a", attrs={"y": True}, step=9)
    await seed_records(merged, records, merge=True)
    combined = await merged.get_node(GraphNodeId("host:a"))
    assert combined is not None
    assert combined.properties["attrs"] == {"x": True, "y": True}


# -- P3: removal exists, and is scoped ------------------------------------


async def test_removing_a_node_drops_its_edges_too() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    svc_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    await svc.link(GraphEdgeType.HOSTS, host.id, svc_node.id)

    assert await svc.remove_node(host.id) is True

    assert await svc.get_node(host.id) is None
    assert await svc.list_edges() == []
    # ...and no dangling reference is left behind.
    assert (await svc.integrity_report()).dangling_edges == []
    assert await svc.remove_node(host.id) is False


async def test_removing_an_edge_leaves_its_nodes_alone() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    svc_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    edge = await svc.link(GraphEdgeType.HOSTS, host.id, svc_node.id)

    assert await svc.remove_edge(edge.id) is True
    assert await svc.neighbors(host.id) == []
    assert await svc.get_node(host.id) is not None


def test_the_in_memory_store_satisfies_both_optional_protocols() -> None:
    store = InMemoryAttackGraphStore()
    assert isinstance(store, ListableAttackGraphStore)
    assert isinstance(store, MutableAttackGraphStore)


# -- the two paths that had no test at all --------------------------------


def test_a_url_target_gets_a_normalised_endpoint_id() -> None:
    node_id = node_id_for_target(
        __import__("redteam.core.contracts.target", fromlist=["UrlTarget"]).UrlTarget(
            url="https://api.local/orders/8831?page=2"
        )
    )
    assert str(node_id) == "endpoint:https://api.local/orders/{id}"


async def test_findings_show_up_in_the_observation() -> None:
    from redteam.core.contracts.graph import Finding

    svc = service()
    await svc.record_finding(
        Finding(
            finding_id="f1", title="t", severity="high", verified=True, evidence_ids=["ev"]
        )
    )
    observation = await svc.observe(step=1)
    assert observation.findings == ["f1 [high/verified]"]
