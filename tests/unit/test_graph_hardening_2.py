"""Second hardening pass: merge correctness, unbounded growth, silent skips."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import InvalidNodeIdError, make_node_id
from redteam.attack_graph.observation import ATTEMPTED_FACT_KEY, build_observation
from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import (
    MAX_FACT_LIST_ITEMS,
    MAX_PROVENANCE_ENTRIES,
    Provenance,
    attrs_of,
    build_properties,
    facts_of,
    provenance_of,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.snapshot import SnapshotError, from_records
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.traversal import evidence_chain
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId

NOW = datetime.now(timezone.utc)


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


# -- a withdrawn predicate must not come back -----------------------------


async def test_importing_an_older_snapshot_does_not_resurrect_a_retraction() -> None:
    # Sticky-True merge used to undo a retraction on any later stale
    # observation, including a snapshot import.
    svc = service()
    node = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    stale = await svc.export_snapshot()
    await svc.retract(node.id, "reachable", step=5, reason="stopped answering")

    await svc.import_snapshot(stale, merge=True)

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert "reachable" not in attrs_of(refreshed.properties)


async def test_a_newer_observation_still_restores_a_retracted_predicate() -> None:
    # The block is on *older* evidence only; re-observing is how a
    # predicate legitimately comes back.
    svc = service()
    node = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await svc.retract(node.id, "reachable", step=5, reason="stopped answering")

    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=9)

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert attrs_of(refreshed.properties)["reachable"] is True


# -- an edge constraint must not vanish with its counterpart --------------


def test_an_edge_constraint_whose_counterpart_is_missing_fails_loudly() -> None:
    # With `edge_absent` this is an authorization-boundary check; letting
    # it disappear because the other bind was optional means the step is
    # offered exactly when it should not be.
    nodes = [
        GraphNode(
            id=GraphNodeId("host:a"),
            type=GraphNodeType.HOST,
            properties=build_properties(),
            created_at=NOW,
            updated_at=NOW,
        )
    ]
    patterns = [
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="p", optional=True),
        NodePattern(
            type=GraphNodeType.HOST,
            bind="h",
            related_to="p",
            edge_type=GraphEdgeType.CAN_ACCESS,
        ),
    ]

    result = resolve(patterns, nodes=nodes, edges=[])

    assert not result.ok
    assert "has no candidates" in result.reason


def test_an_optional_owner_of_a_dead_edge_constraint_is_just_skipped() -> None:
    nodes = [
        GraphNode(
            id=GraphNodeId("host:a"),
            type=GraphNodeType.HOST,
            properties=build_properties(),
            created_at=NOW,
            updated_at=NOW,
        )
    ]
    patterns = [
        NodePattern(type=GraphNodeType.HOST, bind="h"),
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="p", optional=True),
        NodePattern(
            type=GraphNodeType.CREDENTIAL,
            bind="c",
            optional=True,
            related_to="p",
            edge_type=GraphEdgeType.CAN_ACCESS,
        ),
    ]

    result = resolve(patterns, nodes=nodes, edges=[])

    assert result.ok
    assert set(result.bindings) == {"h"}


# -- nothing on a node grows without bound --------------------------------


async def test_provenance_is_capped_but_keeps_the_first_entry() -> None:
    svc = service()
    for step in range(200):
        await svc.upsert_node(
            GraphNodeType.HOST,
            key="a.local",
            provenance=Provenance(step=step, source=f"tool:{step}"),
            step=step,
        )

    node = await svc.get_node(GraphNodeId("host:a.local"))
    assert node is not None
    entries = provenance_of(node.properties)
    assert len(entries) == MAX_PROVENANCE_ENTRIES
    assert entries[0]["source"] == "tool:0"  # who first saw it survives
    assert entries[-1]["source"] == "tool:199"


async def test_the_attempt_record_is_never_truncated() -> None:
    # It was capped for merge cost, but it is also what decides whether to
    # act — trimming it re-runs work already done.
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    total = MAX_FACT_LIST_ITEMS + 50
    for index in range(total):
        await svc.record_attempt(node.id, f"cap{index}/op")

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    kept = facts_of(refreshed.properties)[ATTEMPTED_FACT_KEY]
    assert len(kept) == total
    assert kept[0] == "cap0/op"
    assert await svc.has_attempted(node.id, "cap0/op") is True


async def test_recording_attempts_stays_cheap_at_realistic_scale() -> None:
    # The full record is kept, so cost grows faster than linear. What
    # matters is the realistic range: tens of distinct operations against
    # one node, not thousands.
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    started = time.perf_counter()
    for index in range(50):
        await svc.record_attempt(node.id, f"cap{index}/op")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert await svc.has_attempted(node.id, "cap0/op") is True


async def test_re_recording_a_known_attempt_is_a_no_op() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    await svc.record_attempt(node.id, "cap/op")
    before = await svc.get_node(node.id)

    await svc.record_attempt(node.id, "cap/op")

    after = await svc.get_node(node.id)
    assert before is not None and after is not None
    assert before.updated_at == after.updated_at


# -- cleanup status only moves forward ------------------------------------


async def test_re_registering_a_cleaned_resource_does_not_reopen_it() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.ASSET, key="tmp-file")
    await svc.register_created_resource(node.id, resource_type="file")
    await svc.mark_cleaned(node.id, evidence_id="ev_1")

    await svc.register_created_resource(node.id, resource_type="file")

    assert await svc.pending_cleanup() == []


async def test_reopen_is_available_when_the_resource_really_was_recreated() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.ASSET, key="tmp-file")
    await svc.register_created_resource(node.id, resource_type="file")
    await svc.mark_cleaned(node.id, evidence_id="ev_1")

    await svc.register_created_resource(node.id, resource_type="file", reopen=True)

    assert len(await svc.pending_cleanup()) == 1


# -- keys and ids stay usable ---------------------------------------------


@pytest.mark.parametrize("key", ["", "   ", "a\nb", "a\x00b"])
def test_blank_and_control_character_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError):
        build_properties(attrs={key: True})


@pytest.mark.parametrize("key", ["", "   ", "a\nb", "host\x00"])
def test_node_ids_reject_blank_and_control_characters(key: str) -> None:
    # A newline or NUL in an id corrupts every line-oriented consumer.
    with pytest.raises(InvalidNodeIdError):
        make_node_id(GraphNodeType.HOST, key)


# -- third pass: display vs identity, strict predicates, silent truncation --


def test_node_ids_survive_a_key_containing_the_flag_separator() -> None:
    # `nodes_by_type` renders "<key><+flags>"; a URL query legitimately
    # contains "+", so ids must not be parsed back out of that string.
    node = GraphNode(
        id=GraphNodeId("endpoint:a.local GET /search?q=a+b"),
        type=GraphNodeType.ENDPOINT,
        properties=build_properties(attrs={"reachable": True}),
        created_at=NOW,
        updated_at=NOW,
    )
    observation = build_observation([node])

    assert observation.node_ids(GraphNodeType.ENDPOINT) == [node.id]
    # the display string still carries the flags
    assert observation.nodes_by_type["endpoint"][0].endswith("+reachable")


def test_a_boolean_predicate_does_not_match_an_integer() -> None:
    # Plain `==` makes True == 1 in Python, so a flag pattern would match
    # a node carrying a count.
    counted = GraphNode(
        id=GraphNodeId("host:a"),
        type=GraphNodeType.HOST,
        properties=build_properties(attrs={"flag": 1}),
        created_at=NOW,
        updated_at=NOW,
    )
    flagged = GraphNode(
        id=GraphNodeId("host:b"),
        type=GraphNodeType.HOST,
        properties=build_properties(attrs={"flag": True}),
        created_at=NOW,
        updated_at=NOW,
    )
    pattern = [NodePattern(type=GraphNodeType.HOST, attrs={"flag": True}, bind="h")]

    assert not resolve(pattern, nodes=[counted]).ok
    assert resolve(pattern, nodes=[flagged]).ok


def test_a_chain_cut_short_by_the_depth_limit_says_so() -> None:
    nodes = [
        GraphNode(
            id=GraphNodeId("finding:f"),
            type=GraphNodeType.FINDING,
            properties=build_properties(
                facts={"finding_id": "f", "verified": True, "evidence_ids": ["ev"]}
            ),
            created_at=NOW,
            updated_at=NOW,
        )
    ]
    edges: list[GraphEdge] = []
    previous = "finding:f"
    for index in range(20):
        node_id = f"hypothesis:h{index}"
        nodes.append(
            GraphNode(
                id=GraphNodeId(node_id),
                type=GraphNodeType.HYPOTHESIS,
                properties=build_properties(facts={"hypothesis_id": f"h{index}"}),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        edges.append(
            GraphEdge(
                id=GraphEdgeId(f"e{index}"),
                type=GraphEdgeType.SUPPORTED_BY,
                source_id=GraphNodeId(previous),
                target_id=GraphNodeId(node_id),
                created_at=NOW,
            )
        )
        previous = node_id

    chain = evidence_chain(GraphNodeId("finding:f"), nodes=nodes, edges=edges)

    assert chain is not None
    assert chain.truncated is True
    assert "truncated" in chain.describe()


async def test_a_complete_chain_is_not_marked_truncated() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    await svc.record_hypothesis(
        Hypothesis(hypothesis_id="hy", description="d"), about=host.id
    )
    await svc.record_finding(
        Finding(finding_id="f", title="t", verified=True, evidence_ids=["ev"]),
        hypothesis_id="hy",
        about=host.id,
    )

    chain = evidence_chain(
        GraphNodeId("finding:f"),
        nodes=await svc.list_nodes(),
        edges=await svc.list_edges(),
    )

    assert chain is not None and chain.truncated is False


async def test_a_snapshot_without_a_header_is_refused() -> None:
    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    records = await svc.export_snapshot()
    headerless = [r for r in records if r["_collection"] != "graph_meta"]

    # No header means no version to check, so a future schema would load
    # silently and wrongly.
    with pytest.raises(SnapshotError, match="schema version cannot be checked"):
        from_records(headerless)


def test_an_empty_record_list_is_still_an_empty_graph() -> None:
    assert from_records([]) == ([], [], {})
