import pytest

from redteam_core.graph import frontier
from tests.performance.graph_adapter import graph_input
from tests.performance.oracles import Edge


@pytest.mark.parametrize("has_sink", [False, True])
def test_sink_memo_preserves_result_and_search_local_negative_cache(monkeypatch, has_sink):
    edges = (
        Edge("a", "shared", "source", verdict="FALSE"),
        Edge("b", "shared", "source", verdict="FALSE"),
    )
    if has_sink:
        edges += (Edge("shared", "target", "sink"),)
    session, cat = graph_input(edges)
    original = frontier._nearest_sink
    calls = []

    def counted(start, adjacency, catalog, depth):
        calls.append((start, depth))
        return original(start, adjacency, catalog, depth)

    monkeypatch.setattr(frontier, "_nearest_sink", counted)
    baseline = frontier.build_attack_frontiers(session, catalog=cat, memoize_sink=False)
    assert len(calls) == 2
    calls.clear()
    cached = frontier.build_attack_frontiers(session, catalog=cat, memoize_sink=True)
    assert cached == baseline
    assert len(calls) == len(set(calls)) == 1
    calls.clear()
    assert frontier.build_attack_frontiers(session, catalog=cat, memoize_sink=True) == baseline
    assert len(calls) == 1  # no cross-search cache, including cached None
