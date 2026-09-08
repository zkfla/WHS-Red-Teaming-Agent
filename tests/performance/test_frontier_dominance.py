from dataclasses import replace
from random import Random

import pytest

from redteam_core.graph.frontier import _dominates, _PathState, build_attack_frontiers
from tests.performance.graph_adapter import core_reachability, graph_input
from tests.performance.oracles import Edge, Guard, graph_oracle


def state(nodes=("s", "a", "z"), relations=("entry", "flow")):
    return _PathState(
        nodes=nodes,
        relation_ids=relations,
        source_tags=("input",),
        mechanism_tags=("flow",),
        sink_tags=(),
        context_refs=(),
        guard_relation_ids=(),
        uncertain_guard_relation_ids=(),
        missing_reason_ids=(),
        requirement_ids=(),
        unknown_relation_ids=(),
        influence_proven=True,
        evidence_ids=(),
        continue_after_sink=False,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"nodes": ("other-source", "a", "z")},
        {"blocked": True},
        {"influence_proven": False},
        {"continue_after_sink": True},
        {"context_refs": ("other",)},
        {"guard_relation_ids": ("guard",)},
        {"uncertain_guard_relation_ids": ("guard",)},
        {"missing_reason_ids": ("missing",)},
        {"requirement_ids": ("requirement",)},
        {"unknown_relation_ids": ("unknown",)},
        {"evidence_ids": ("evidence",)},
        {"relation_ids": ("different-entry", "flow")},
    ],
)
def test_noninterchangeable_state_cannot_dominate(change):
    short = state()
    long = state(("s", "a", "b", "z"), ("entry", "detour", "end"))
    assert _dominates(short, long)
    different = replace(long, **change)
    assert not _dominates(short, different)
    assert not _dominates(different, short)


def test_visited_sets_and_ties_use_strict_partial_order():
    left = state(("s", "a", "z"), ("entry", "a"))
    right = state(("s", "b", "z"), ("entry", "b"))
    assert not _dominates(left, right) and not _dominates(right, left)
    assert not _dominates(left, left)
    tie = replace(left, relation_ids=("entry", "b"))
    assert _dominates(left, tie) and not _dominates(tie, left)


def test_distinct_evidence_routes_are_not_discarded():
    edges = (
        Edge("s", "a", "source"),
        Edge("a", "b"),
        Edge("a", "c"),
        Edge("c", "b"),
        Edge("b", "z", "sink"),
    )
    session, cat = graph_input(edges)
    for key, relation in tuple(session.semantic.relations.items()):
        if relation.source_ref == "a" and relation.target_ref == "b":
            session.semantic.relations[key] = relation.model_copy(
                update={"evidence_ids": ["evidence:direct"]},
            )
    result = build_attack_frontiers(session, catalog=cat)
    assert len([f for f in result.frontiers if f.sink_ref == "z"]) == 2


@pytest.mark.parametrize("seed", range(250))
@pytest.mark.parametrize("budget_version", ["legacy", "v2"])
def test_seeded_small_graphs_match_exhaustive_oracle(seed, budget_version):
    rng = Random(seed)
    nodes = [f"n{i}" for i in range(5)]
    contexts = [(), ("one",), ("two",)]
    edges = [Edge("s", "n0", "source", contexts=rng.choice(contexts))]
    if seed % 2:
        edges.append(Edge("t", "n0", "source", contexts=rng.choice(contexts)))
    for source in nodes:
        for target in nodes:
            if source != target and rng.random() < 0.25 and len(edges) < 9:
                edges.append(
                    Edge(
                        source,
                        target,
                        rng.choice(("flow", "flow", "bridge")),
                        rng.choice(("TRUE", "TRUE", "FALSE", "UNKNOWN")),
                        rng.choice(contexts),
                    )
                )
    edges.append(Edge(rng.choice(nodes), "z", "sink", contexts=rng.choice(contexts)))
    guards = (
        Guard(
            rng.choice(nodes),
            rng.choice(("BLOCKS", "MAY_BLOCK", "INFORMATIONAL")),
            rng.choice(("TRUE", "FALSE", "UNKNOWN")),
            rng.choice(contexts),
        ),
    )
    expected = graph_oracle(tuple(edges), guards)
    actual, result = core_reachability(tuple(edges), guards, budget_version=budget_version)
    assert result.search_complete and not result.truncated
    assert actual == expected, {"seed": seed, "edges": edges, "guards": guards}
