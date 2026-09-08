from itertools import permutations

import pytest

from redteam_core.graph.frontier import FrontierQuery, _SinkCache, build_attack_frontiers
from tests.performance.graph_adapter import graph_input
from tests.performance.oracles import Edge, Guard

EDGES = (Edge("s", "a", "source"), Edge("a", "b", "flow"), Edge("b", "t", "sink"))


def run(edges=EDGES, guards=(), **limits):
    session, cat = graph_input(edges, guards)
    before = session.model_copy(deep=True)
    result = build_attack_frontiers(
        session, catalog=cat, query=FrontierQuery(budget_version="v2", **limits)
    )
    assert session == before
    assert result.work_units_used == sum(result.work_counters.values())
    return result


def test_v2_unlimited_matches_legacy_semantics_and_expansions():
    session, cat = graph_input(EDGES)
    baseline = build_attack_frontiers(session, catalog=cat)
    result = run()
    assert result.frontiers == baseline.frontiers
    assert result.expansions_used == baseline.expansions_used
    assert result.search_complete and not result.truncated
    assert not result.stop_reasons
    for stage in (
        "relation_input",
        "adjacency_index",
        "source_scan",
        "source_initialization",
        "guard_index",
        "queue_pop",
        "edge_check",
    ):
        assert result.work_counters[stage] > 0


@pytest.mark.parametrize("budget", range(35))
def test_work_limit_never_overshoots_or_reports_incomplete_as_complete(budget):
    result = run(max_work_units=budget)
    assert result.work_units_used <= budget
    if "MAX_WORK_UNITS" in result.stop_reasons:
        assert result.truncated and not result.search_complete
        assert not result.unexplored_source_count_known


def test_input_is_rejected_before_partial_index_when_budget_too_small():
    result = run(max_work_units=2)
    assert result.work_units_used == 0
    assert not result.frontiers
    assert result.stop_reasons == ("MAX_WORK_UNITS",)


@pytest.mark.parametrize("capacity", [0, 1, 2])
def test_initial_sources_obey_queue_budget_even_with_zero_expansions(capacity):
    edges = tuple(Edge(f"s{i}", "a", "source") for i in range(3))
    result = run(edges, max_queue_states=capacity, max_expansions=0)
    assert result.queue_peak <= capacity
    assert result.stop_reasons == ("MAX_QUEUE_STATES",)
    assert result.truncated and not result.search_complete


def test_legacy_expansion_limit_remains_explicit_in_v2():
    result = run(max_expansions=0)
    assert result.expansions_used == 0
    assert result.stop_reasons == ("MAX_EXPANSIONS",)
    assert result.work_counters["source_initialization"] == 1


def test_guard_and_auxiliary_work_are_charged():
    result = run(guards=(Guard("a", "BLOCKS", "TRUE"),))
    assert result.work_counters["guard_check"] == 1
    assert result.aux_visits_used > 0
    limited = run(guards=(Guard("a", "BLOCKS", "TRUE"),), max_aux_visits=0)
    assert limited.stop_reasons == ("MAX_AUX_VISITS",)
    assert limited.aux_visits_used == 0
    assert limited.truncated


def test_fanout_queue_and_completed_output_are_bounded():
    edges = (Edge("s", "a", "source"), *(Edge("a", f"t{i}", "sink") for i in range(6)))
    result = run(edges, max_queue_states=2)
    assert result.queue_peak <= 2
    assert "MAX_QUEUE_STATES" in result.stop_reasons
    result = run(edges, max_frontiers=2)
    assert len(result.frontiers) == 2
    assert "MAX_FRONTIERS" in result.stop_reasons


def test_input_permutation_preserves_limited_result_and_counters():
    results = [run(edges, max_work_units=17) for edges in permutations(EDGES)]
    assert all(result == results[0] for result in results)


def test_cache_admission_has_hard_item_cap_and_caches_none():
    cache = _SinkCache(1)
    cache[("a", 1)] = None
    cache[("b", 1)] = "sink"
    assert len(cache) == 1 and ("a", 1) in cache
    assert cache.admission_refused
    byte_limited = _SinkCache(10, max_bytes=9)
    byte_limited[("a", 1)] = None
    byte_limited[("b", 1)] = "sink"
    assert byte_limited.used_bytes == 9 and len(byte_limited) == 1
    assert byte_limited.admission_refused


def test_explicit_legacy_mode_does_not_silently_apply_v2_limits():
    session, cat = graph_input(EDGES)
    result = build_attack_frontiers(
        session, catalog=cat, query=FrontierQuery(max_work_units=0, max_queue_states=0)
    )
    assert result.budget_version == "legacy"
    assert result.search_complete and result.work_counters == {}


def test_main_and_auxiliary_queue_share_one_capacity():
    edges = tuple(Edge(f"s{i}", "a", "source", verdict="FALSE") for i in range(3))
    edges += (
        Edge("a", "x", "flow"),
        Edge("a", "y", "flow"),
        Edge("x", "t", "sink"),
        Edge("y", "t", "sink"),
    )
    result = run(edges, max_queue_states=3)
    assert result.queue_peak <= 3
    assert result.stop_reasons == ("MAX_QUEUE_STATES",)
    assert result.truncated and result.aux_visits_used > 0


def test_cache_capacity_fallback_preserves_semantics_and_is_reported():
    edges = (Edge("s", "a", "source", verdict="FALSE"), Edge("a", "t", "sink"))
    session, cat = graph_input(edges)
    baseline = build_attack_frontiers(session, catalog=cat)
    limited = build_attack_frontiers(
        session, catalog=cat, memoize_sink=True, max_sink_cache_bytes=0
    )
    assert limited.frontiers == baseline.frontiers
    assert limited.sink_memo_capacity_reached
    assert limited.search_complete == baseline.search_complete


def test_cache_capacity_metadata_survives_later_work_stop():
    edges = (
        Edge("s", "a", "source", verdict="FALSE"),
        Edge("t", "a", "source", verdict="FALSE"),
        Edge("a", "z", "sink"),
    )
    session, cat = graph_input(edges)
    result = build_attack_frontiers(
        session,
        catalog=cat,
        memoize_sink=True,
        max_sink_cache_entries=0,
        query=FrontierQuery(budget_version="v2", max_aux_visits=2),
    )
    assert result.sink_memo_capacity_reached
    assert result.stop_reasons == ("MAX_AUX_VISITS",)
    assert len(result.frontiers) == 1
