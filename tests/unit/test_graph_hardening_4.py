"""Fourth hardening pass: correctness issues found in external review.

Every test here reproduces something that was wrong and shipped.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import OBSERVED_TARGET_FACT, target_for_node
from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import (
    SensitiveValueError,
    attrs_of,
    build_properties,
    facts_of,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode
from redteam.core.contracts.target import EndpointTarget, UrlTarget
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId

NOW = datetime.now(timezone.utc)


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


def node(node_id: str, node_type: GraphNodeType) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=build_properties(),
        created_at=NOW,
        updated_at=NOW,
    )


# -- reads must not hand out the stored object ----------------------------


async def test_mutating_a_read_node_does_not_reach_the_store() -> None:
    svc = service()
    stored = await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"safe": True})

    fetched = await svc.get_node(stored.id)
    assert fetched is not None
    fetched.properties["attrs"]["injected"] = True

    again = await svc.get_node(stored.id)
    assert again is not None
    assert "injected" not in attrs_of(again.properties)


async def test_the_read_only_view_cannot_be_used_to_write_either() -> None:
    # The view has no write methods, but handing back the live object made
    # that boundary decorative.
    svc = service()
    stored = await svc.upsert_node(GraphNodeType.HOST, key="a.local")

    fetched = await svc.view().get_node(stored.id)
    assert fetched is not None
    fetched.properties["attrs"]["injected"] = True

    again = await svc.get_node(stored.id)
    assert again is not None
    assert "injected" not in attrs_of(again.properties)


async def test_mutating_what_was_written_does_not_reach_the_store() -> None:
    svc = service()
    returned = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    returned.properties["attrs"]["injected"] = True

    again = await svc.get_node(returned.id)
    assert again is not None
    assert "injected" not in attrs_of(again.properties)


async def test_listings_hand_out_copies_too() -> None:
    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    (await svc.list_nodes())[0].properties["attrs"]["injected"] = True
    (await svc.query(node_type=GraphNodeType.HOST))[0].properties["attrs"]["q"] = True

    stored = await svc.get_node(GraphNodeId("host:a.local"))
    assert stored is not None
    assert attrs_of(stored.properties) == {}


# -- grounding links are never trimmed ------------------------------------


async def test_evidence_ids_survive_an_update_past_the_list_cap() -> None:
    # The fact-list cap exists to stop an attempt log growing forever; it
    # must not reach a finding's evidence, which is what grounds it.
    svc = service()
    first = [f"ev_{index:04d}" for index in range(150)]
    second = [f"ev_{index:04d}" for index in range(150, 260)]
    await svc.record_finding(
        Finding(finding_id="f", title="t", verified=True, evidence_ids=first)
    )
    await svc.record_finding(
        Finding(finding_id="f", title="t", verified=True, evidence_ids=second)
    )

    found = await svc.get_finding("f")
    assert found is not None
    assert len(found.evidence_ids) == 260
    assert "ev_0000" in found.evidence_ids


async def test_the_attempt_log_keeps_everything_it_is_asked_about() -> None:
    svc = service()
    target = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    for index in range(250):
        await svc.record_attempt(target.id, f"cap{index}/op")

    stored = await svc.get_node(target.id)
    assert stored is not None
    assert len(facts_of(stored.properties)["attempted_operations"]) == 250
    # The oldest entry is exactly the one a cap would have dropped.
    assert await svc.has_attempted(target.id, "cap0/op") is True
    assert await svc.has_attempted(target.id, "cap249/op") is True


# -- "already attempted" must be authoritative ----------------------------


async def test_attempts_are_answered_from_the_graph_not_the_capped_summary() -> None:
    # The observation caps `attempted` for prompt size. Deciding whether to
    # act from that list re-runs whatever fell off the end.
    svc = service()
    ids = []
    for index in range(45):
        created = await svc.upsert_node(
            GraphNodeType.HOST, key=f"h{index:02d}.local", attrs={"in_scope": True}, step=1
        )
        ids.append(created.id)
        await svc.record_attempt(created.id, "recon.example/ping", step=2)

    observation = await svc.observe(step=3)
    from_summary = sum(
        1 for node_id in ids if observation.has_attempted(f"recon.example/ping@{node_id}")
    )
    assert from_summary < len(ids)  # the cap bites

    for node_id in ids:
        assert await svc.has_attempted(node_id, "recon.example/ping") is True
    assert await svc.has_attempted(ids[0], "recon.example/other") is False
    assert await svc.has_attempted(GraphNodeId("host:ghost"), "x/y") is False


# -- an edge constraint must hold for every bound node --------------------


def test_every_node_bound_to_an_edge_pattern_must_carry_the_relation() -> None:
    # With count=2, "owns these two assets" has to mean both; `any` let a
    # node the principal has no claim on satisfy the precondition.
    nodes = [
        node("principal:p", GraphNodeType.PRINCIPAL),
        node("asset:a1", GraphNodeType.ASSET),
        node("asset:a2", GraphNodeType.ASSET),
    ]
    owns_one = [
        GraphEdge(
            id=GraphEdgeId("e1"),
            type=GraphEdgeType.OWNS,
            source_id=GraphNodeId("principal:p"),
            target_id=GraphNodeId("asset:a1"),
            created_at=NOW,
        )
    ]
    patterns = [
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="p"),
        NodePattern(
            type=GraphNodeType.ASSET,
            bind="owned",
            count=2,
            related_to="p",
            edge_type=GraphEdgeType.OWNS,
        ),
    ]

    assert not resolve(patterns, nodes=nodes, edges=owns_one).ok

    owns_both = owns_one + [
        GraphEdge(
            id=GraphEdgeId("e2"),
            type=GraphEdgeType.OWNS,
            source_id=GraphNodeId("principal:p"),
            target_id=GraphNodeId("asset:a2"),
            created_at=NOW,
        )
    ]
    assert resolve(patterns, nodes=nodes, edges=owns_both).ok


def test_edge_absent_still_requires_none_of_them_linked() -> None:
    nodes = [
        node("principal:p", GraphNodeType.PRINCIPAL),
        node("asset:a1", GraphNodeType.ASSET),
        node("asset:a2", GraphNodeType.ASSET),
    ]
    edges = [
        GraphEdge(
            id=GraphEdgeId("e1"),
            type=GraphEdgeType.OWNS,
            source_id=GraphNodeId("principal:p"),
            target_id=GraphNodeId("asset:a1"),
            created_at=NOW,
        )
    ]
    patterns = [
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="p"),
        NodePattern(
            type=GraphNodeType.ASSET,
            bind="not_owned",
            count=2,
            related_to="p",
            edge_type=GraphEdgeType.OWNS,
            edge_absent=True,
        ),
    ]
    assert not resolve(patterns, nodes=nodes, edges=edges).ok


# -- a retraction arriving by merge must actually withdraw ----------------


async def test_merging_a_snapshot_that_records_a_retraction_withdraws_it() -> None:
    source = service()
    created = await source.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await source.retract(created.id, "reachable", step=5, reason="stopped answering")
    records = await source.export_snapshot()

    destination = service()
    await destination.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await destination.import_snapshot(records, merge=True)

    merged = await destination.get_node(created.id)
    assert merged is not None
    assert "reachable" not in attrs_of(merged.properties)


async def test_an_observation_newer_than_the_retraction_still_wins() -> None:
    source = service()
    created = await source.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await source.retract(created.id, "reachable", step=5, reason="stopped answering")
    records = await source.export_snapshot()

    destination = service()
    await destination.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=9
    )
    await destination.import_snapshot(records, merge=True)

    merged = await destination.get_node(created.id)
    assert merged is not None
    assert attrs_of(merged.properties)["reachable"] is True


# -- secrets nested inside a value ----------------------------------------


@pytest.mark.parametrize(
    "facts",
    [
        {"metadata": {"password": "hunter2"}},
        {"creds": [{"api_key": "abc"}]},
        {"a": {"b": {"token": "xyz"}}},
        {"meta": {"pan": "4242424242424242"}},
        {"list_of_pans": ["4242424242424242"]},
    ],
)
def test_a_secret_one_level_down_is_still_rejected(facts: dict) -> None:
    with pytest.raises(SensitiveValueError):
        build_properties(facts=facts)


async def test_a_nested_secret_never_reaches_a_snapshot_or_a_render() -> None:
    from redteam.attack_graph.visualize import render_html

    svc = service()
    with pytest.raises(SensitiveValueError):
        await svc.upsert_node(
            GraphNodeType.HOST, key="a.local", facts={"metadata": {"password": "hunter2"}}
        )

    assert "hunter2" not in str(await svc.export_snapshot())
    assert "hunter2" not in render_html(await svc.list_nodes(), await svc.list_edges())


def test_ordinary_nested_values_still_pass() -> None:
    assert build_properties(facts={"headers": {"content-type": "application/json"}})
    assert build_properties(facts={"ports": [22, 80, 443]})


# -- the node id is an identity, not an address ---------------------------


async def test_the_address_actually_observed_is_kept_alongside_the_id() -> None:
    # Ids normalise `/orders/8831` to `/orders/{id}` so one endpoint stays
    # one node. Acting on that template would request it literally.
    svc = service()
    target = UrlTarget(url="https://api.local/orders/8831")

    created = await svc.observe_target(target, step=1)
    observed = facts_of(created.properties)[OBSERVED_TARGET_FACT]

    assert str(created.id) == "endpoint:https://api.local/orders/{id}"
    assert observed == target.url
    rebuilt = target_for_node(created.id, created.type, observed=observed)
    assert isinstance(rebuilt, UrlTarget)
    assert rebuilt.url == target.url


async def test_a_second_sighting_of_the_same_endpoint_is_the_same_node() -> None:
    svc = service()
    first = await svc.observe_target(UrlTarget(url="https://api.local/orders/1"), step=1)
    second = await svc.observe_target(UrlTarget(url="https://api.local/orders/2"), step=2)
    assert first.id == second.id


async def test_endpoint_targets_keep_their_observed_path() -> None:
    svc = service()
    created = await svc.observe_target(
        EndpointTarget(host="api.local", port=8443, path="/orders/8831"), step=1
    )
    observed = facts_of(created.properties)[OBSERVED_TARGET_FACT]
    rebuilt = target_for_node(created.id, created.type, observed=observed)
    assert isinstance(rebuilt, EndpointTarget)
    assert rebuilt.path == "/orders/8831"
    assert rebuilt.port == 8443


def test_without_the_observed_address_the_template_is_returned() -> None:
    rebuilt = target_for_node(
        GraphNodeId("endpoint:https://api.local/orders/{id}"), GraphNodeType.ENDPOINT
    )
    assert isinstance(rebuilt, UrlTarget)
    assert rebuilt.url == "https://api.local/orders/{id}"
