import pytest

from redteam_core.graph import frontier
from tests.performance.graph_adapter import graph_input
from tests.performance.oracles import Edge


@pytest.mark.parametrize("budget_version", ["legacy", "v2"])
def test_withheld_default_preserves_both_budget_contracts(monkeypatch, budget_version):
    session, cat = graph_input(
        (
            Edge("a", "shared", "source", verdict="FALSE"),
            Edge("b", "shared", "source", verdict="FALSE"),
            Edge("shared", "target", "sink"),
        )
    )
    original = frontier._nearest_sink
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(frontier, "_nearest_sink", counted)
    query = frontier.FrontierQuery(budget_version=budget_version)
    default = frontier.build_attack_frontiers(session, catalog=cat, query=query)
    assert len(calls) == 2
    calls.clear()
    explicit = frontier.build_attack_frontiers(
        session,
        catalog=cat,
        query=query,
        memoize_sink=False,
    )
    assert explicit == default
    assert len(calls) == 2
    calls.clear()
    off = frontier.build_attack_frontiers(session, catalog=cat, query=query, memoize_sink=False)
    assert len(calls) == 2
    # Work counters differ intentionally in v2; legacy output is identical.
    if budget_version == "legacy":
        assert off == default
    calls.clear()
    frontier.build_attack_frontiers(session, catalog=cat, query=query, memoize_sink=True)
    assert len(calls) == 1  # Optimization remains available to explicitly composed callers.


@pytest.mark.parametrize("invalid", [0, 1, "true", (), []])
def test_invalid_memo_flag_is_rejected(invalid):
    session, cat = graph_input(())
    with pytest.raises(ValueError, match="memoize_sink"):
        frontier.build_attack_frontiers(session, catalog=cat, memoize_sink=invalid)
