"""Precondition matching: the frontier calculation the planner depends on."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import build_properties
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId

NOW = datetime.now(timezone.utc)


def node(
    node_id: str, node_type: GraphNodeType, attrs: dict | None = None, step: int = 0
) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=build_properties(attrs=attrs or {}, step=step),
        created_at=NOW,
        updated_at=NOW,
    )


def edge(source: str, target: str, edge_type: GraphEdgeType) -> GraphEdge:
    return GraphEdge(
        id=GraphEdgeId(f"{edge_type.value}:{source}->{target}"),
        type=edge_type,
        source_id=GraphNodeId(source),
        target_id=GraphNodeId(target),
        created_at=NOW,
    )


def test_resolves_a_simple_pattern() -> None:
    nodes = [node("host:a", GraphNodeType.HOST, {"in_scope": True})]
    result = resolve(
        [NodePattern(type=GraphNodeType.HOST, attrs={"in_scope": True}, bind="h")],
        nodes=nodes,
    )
    assert result.ok
    assert result.one("h") == GraphNodeId("host:a")


def test_unmet_precondition_explains_itself() -> None:
    result = resolve([NodePattern(type=GraphNodeType.HOST, attrs={"in_scope": True})], nodes=[])
    assert not result.ok
    assert "needs 1, found 0" in result.reason


def test_optional_pattern_is_skipped_rather_than_failing() -> None:
    nodes = [node("host:a", GraphNodeType.HOST)]
    result = resolve(
        [
            NodePattern(type=GraphNodeType.HOST, bind="h"),
            NodePattern(type=GraphNodeType.CREDENTIAL, bind="cred", optional=True),
        ],
        nodes=nodes,
    )
    assert result.ok
    assert "cred" not in result.bindings


def test_from_param_pins_the_bind_to_the_chosen_node() -> None:
    # Without this, resolution picks any matching node and the node that
    # gets checked is not the node that was acted on.
    nodes = [node("host:a", GraphNodeType.HOST), node("host:b", GraphNodeType.HOST)]
    pattern = NodePattern(type=GraphNodeType.HOST, bind="h", from_param="h")

    result = resolve([pattern], nodes=nodes, params={"h": "host:b"})
    assert result.one("h") == GraphNodeId("host:b")

    missing = resolve([pattern], nodes=nodes, params={})
    assert not missing.ok
    assert "cannot be pinned" in missing.reason


def test_from_param_is_ignored_when_answering_availability() -> None:
    nodes = [node("host:a", GraphNodeType.HOST)]
    pattern = NodePattern(type=GraphNodeType.HOST, bind="h", from_param="h")
    assert resolve([pattern], nodes=nodes, for_frontier=True).ok


def test_distinct_from_is_symmetric_and_prevents_self_matching() -> None:
    # "transferred to my own account and called it someone else's" is the
    # bug this constraint exists for.
    nodes = [node("principal:me", GraphNodeType.PRINCIPAL)]
    result = resolve(
        [
            NodePattern(type=GraphNodeType.PRINCIPAL, bind="attacker"),
            NodePattern(type=GraphNodeType.PRINCIPAL, bind="victim", distinct_from=["attacker"]),
        ],
        nodes=nodes,
    )
    assert not result.ok

    nodes.append(node("principal:other", GraphNodeType.PRINCIPAL))
    ok = resolve(
        [
            # Declared on one side only — the constraint still binds both.
            NodePattern(type=GraphNodeType.PRINCIPAL, bind="attacker", distinct_from=["victim"]),
            NodePattern(type=GraphNodeType.PRINCIPAL, bind="victim"),
        ],
        nodes=nodes,
    )
    assert ok.ok
    assert ok.one("attacker") != ok.one("victim")


def test_edge_constraint_requires_the_relation() -> None:
    nodes = [node("host:a", GraphNodeType.HOST), node("service:a/ssh", GraphNodeType.SERVICE)]
    patterns = [
        NodePattern(type=GraphNodeType.HOST, bind="h"),
        NodePattern(
            type=GraphNodeType.SERVICE,
            bind="svc",
            related_to="h",
            edge_type=GraphEdgeType.HOSTS,
        ),
    ]
    assert not resolve(patterns, nodes=nodes, edges=[]).ok
    assert resolve(
        patterns, nodes=nodes, edges=[edge("host:a", "service:a/ssh", GraphEdgeType.HOSTS)]
    ).ok


def test_edge_absent_expresses_an_authorization_boundary() -> None:
    # "an asset this principal does NOT own" — not expressible with
    # type+attrs alone.
    nodes = [node("principal:me", GraphNodeType.PRINCIPAL), node("asset:db", GraphNodeType.ASSET)]
    patterns = [
        NodePattern(type=GraphNodeType.PRINCIPAL, bind="me"),
        NodePattern(
            type=GraphNodeType.ASSET,
            bind="theirs",
            related_to="me",
            edge_type=GraphEdgeType.OWNS,
            edge_absent=True,
        ),
    ]
    assert resolve(patterns, nodes=nodes, edges=[]).ok
    assert not resolve(
        patterns, nodes=nodes, edges=[edge("principal:me", "asset:db", GraphEdgeType.OWNS)]
    ).ok


def test_resolution_backtracks_instead_of_being_greedy() -> None:
    # A greedy pass would give the only CAN_ACCESS-linked host to the
    # unconstrained pattern and then report failure.
    nodes = [
        node("host:a", GraphNodeType.HOST),
        node("host:b", GraphNodeType.HOST),
        node("principal:p", GraphNodeType.PRINCIPAL),
    ]
    edges = [edge("principal:p", "host:a", GraphEdgeType.CAN_ACCESS)]
    result = resolve(
        [
            NodePattern(type=GraphNodeType.PRINCIPAL, bind="p"),
            NodePattern(type=GraphNodeType.HOST, bind="free", distinct_from=["linked"]),
            NodePattern(
                type=GraphNodeType.HOST,
                bind="linked",
                related_to="p",
                edge_type=GraphEdgeType.CAN_ACCESS,
            ),
        ],
        nodes=nodes,
        edges=edges,
    )
    assert result.ok
    assert result.one("linked") == GraphNodeId("host:a")
    assert result.one("free") == GraphNodeId("host:b")


def test_count_requires_distinct_nodes() -> None:
    nodes = [node("principal:a", GraphNodeType.PRINCIPAL)]
    patterns = [NodePattern(type=GraphNodeType.PRINCIPAL, bind="racers", count=2)]
    assert not resolve(patterns, nodes=nodes).ok

    nodes.append(node("principal:b", GraphNodeType.PRINCIPAL))
    result = resolve(patterns, nodes=nodes)
    assert result.ok
    assert len(set(result.bindings["racers"])) == 2


def test_stale_observations_must_be_re_observed() -> None:
    nodes = [node("host:a", GraphNodeType.HOST, step=1)]
    pattern = NodePattern(type=GraphNodeType.HOST, bind="h", max_age_steps=3)
    assert resolve([pattern], nodes=nodes, current_step=3).ok
    stale = resolve([pattern], nodes=nodes, current_step=9)
    assert not stale.ok
    assert "older than 3 steps" in stale.reason


def test_one_raises_when_the_bind_is_not_singular() -> None:
    nodes = [node("host:a", GraphNodeType.HOST), node("host:b", GraphNodeType.HOST)]
    result = resolve([NodePattern(type=GraphNodeType.HOST, bind="h", count=2)], nodes=nodes)
    with pytest.raises(KeyError):
        result.one("h")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"edge_type": GraphEdgeType.OWNS},
        {"related_to": "other"},
        {"edge_absent": True},
    ],
)
def test_half_declared_edge_constraints_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        NodePattern(type=GraphNodeType.HOST, **kwargs)
