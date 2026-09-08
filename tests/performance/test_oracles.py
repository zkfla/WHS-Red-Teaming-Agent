from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from redteam_core.investigation import ObservationValidity
from redteam_core.knowledge import CoreSession, apply_observations_atomic, refresh_freshness
from redteam_core.models import TrustClass
from tests.helpers import catalog, observation, requirement
from tests.performance.graph_adapter import core_reachability
from tests.performance.oracles import (
    Edge,
    Guard,
    ObservedValue,
    completion_oracle,
    graph_oracle,
    knowledge_oracle,
    transition_oracle,
)

NOW = datetime(2026, 9, 8, 3, tzinfo=UTC)


@pytest.mark.parametrize("values", [(), ("a",), ("a", "a"), ("a", "b")])
@pytest.mark.parametrize("age", [59, 60, 61])
@pytest.mark.parametrize("inferred", [False, True])
@pytest.mark.parametrize("lookup_mode", ["full_scan", "indexed"])
def test_knowledge_matches_independent_sets(values, age, inferred, lookup_mode):
    req = requirement(max_age_seconds=60, required_snapshot_ref="snap:one")
    rows = tuple(
        observation(
            req=req,
            value=value,
            acquired_at=NOW - timedelta(seconds=age),
            resolver_id=f"reference.resolver.r{i}@1",
            trust_class=TrustClass.INFERRED if inferred else TrustClass.OBSERVED,
            validity=ObservationValidity(snapshot_ref="snap:one", not_after=NOW),
        )
        for i, value in enumerate(values)
    )
    expected = knowledge_oracle(
        tuple(
            ObservedValue(
                r.observation_id,
                r.payload.content_hash,
                r.provenance.acquired_at,
                inferred,
                r.validity.snapshot_ref,
                r.validity.not_after,
            )
            for r in rows
        ),
        now=NOW,
        max_age=60,
        snapshot="snap:one",
    )
    session = CoreSession()
    session.investigation.requirements[req.requirement_id] = req
    apply_observations_atomic(
        session,
        rows,
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
        lookup_mode=lookup_mode,
    )
    refresh_freshness(
        session,
        evaluation_time=NOW,
        expected_revision=session.knowledge.revision,
        lookup_mode=lookup_mode,
    )
    actual = session.knowledge.slots[req.requirement_id].model_dump(mode="json")
    actual.pop("requirement_id")
    assert actual == expected


@pytest.mark.parametrize("expiry_delta,snapshot", [(0, "x"), (-1, "x"), (1, "wrong")])
def test_freshness_expiry_snapshot_edges(expiry_delta, snapshot):
    req = requirement(required_snapshot_ref="x")
    obs = observation(
        req=req,
        value="a",
        acquired_at=NOW - timedelta(seconds=10),
        validity=ObservationValidity(
            snapshot_ref=snapshot,
            not_after=NOW + timedelta(seconds=expiry_delta),
        ),
    )
    expected = knowledge_oracle(
        (
            ObservedValue(
                obs.observation_id,
                obs.payload.content_hash,
                obs.provenance.acquired_at,
                snapshot=snapshot,
                expires_at=obs.validity.not_after,
            ),
        ),
        now=NOW,
        snapshot="x",
    )
    session = CoreSession()
    session.investigation.requirements[req.requirement_id] = req
    apply_observations_atomic(
        session,
        (obs,),
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    actual = session.knowledge.slots[req.requirement_id].model_dump(mode="json")
    actual.pop("requirement_id")
    assert actual == expected


def test_future_observation_is_rejected_atomically():
    req = requirement()
    obs = observation(req=req, value="a", acquired_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future"):
        knowledge_oracle((ObservedValue("x", "a", obs.provenance.acquired_at),), now=NOW)
    session = CoreSession()
    session.investigation.requirements[req.requirement_id] = req
    before = session.model_dump(mode="json")
    with pytest.raises(ValueError, match="later"):
        apply_observations_atomic(
            session,
            (obs,),
            catalog=catalog(),
            evaluation_time=NOW,
            expected_revision=0,
        )
    assert session.model_dump(mode="json") == before


@pytest.mark.parametrize("verdict", ["TRUE", "FALSE", "UNKNOWN"])
@pytest.mark.parametrize(
    "guard",
    [
        (),
        (Guard("b"),),
        (Guard("b", verdict="UNKNOWN"),),
        (Guard("b", effect="MAY_BLOCK"),),
        (Guard("b", contexts=("other",)),),
    ],
)
def test_small_chain_guard_oracle(verdict, guard):
    edges = (
        Edge("s", "a", "source", contexts=("one",)),
        Edge("a", "b", verdict=verdict, contexts=("one",)),
        Edge("b", "z", "sink", contexts=("one",)),
    )
    actual, result = core_reachability(edges, guard)
    assert result.search_complete and not result.truncated
    assert actual == graph_oracle(edges, guard)


@pytest.mark.parametrize("role", ["flow", "bridge", "unregistered"])
def test_context_bridge_and_unregistered_edges(role):
    edges = (
        Edge("s", "a", "source", contexts=("one",)),
        Edge("a", "b", role, contexts=("two",)),
        Edge("b", "z", "sink", contexts=("two",)),
    )
    assert core_reachability(edges)[0] == graph_oracle(edges)


def test_cycles_diamond_and_disconnected_nodes():
    edges = (
        Edge("s", "a", "source"),
        Edge("a", "b"),
        Edge("a", "c"),
        Edge("c", "b"),
        Edge("b", "a"),
        Edge("b", "z", "sink"),
        Edge("unused", "other", "sink"),
    )
    assert core_reachability(edges)[0] == graph_oracle(edges)


def test_distinct_sources_keep_reachability_after_convergence():
    """Strict correctness gate: no xfail, pruning cannot erase a principal."""
    edges = (
        Edge("source:one", "a", "source"),
        Edge("source:two", "a", "source"),
        Edge("a", "b"),
        Edge("b", "z", "sink"),
    )
    expected = graph_oracle(edges)
    assert {r[0] for r in expected} == {"source:one", "source:two"}
    mismatches = []
    for ordering in permutations(edges):
        actual, result = core_reachability(ordering)
        assert result.search_complete and not result.truncated
        if actual != expected:
            mismatches.append((sorted(expected - actual), sorted(actual - expected)))
    assert not mismatches, {
        "mismatched_orderings": len(mismatches),
        "total_orderings": 24,
        "first_missing_and_extra": mismatches[0],
    }


@pytest.mark.parametrize("direct_verdict", ["TRUE", "FALSE"])
def test_nonviable_short_route_must_not_prune_viable_detour(direct_verdict):
    edges = (
        Edge("s", "a", "source"),
        Edge("a", "b", verdict=direct_verdict),
        Edge("a", "c"),
        Edge("c", "b"),
        Edge("b", "z", "sink"),
    )
    expected = graph_oracle(edges)
    assert expected == frozenset({("s", "z", (), "SUPPORTED")})
    mismatches = 0
    for ordering in permutations(edges):
        actual, result = core_reachability(ordering)
        assert result.search_complete and not result.truncated
        mismatches += actual != expected
    assert mismatches == 0, {"mismatched_orderings": mismatches, "total_orderings": 120}


@pytest.mark.parametrize(
    "baseline,observed,authoritative,expected",
    [
        (None, (10, 11), True, "UNKNOWN"),
        ((10, 11), None, True, "UNKNOWN"),
        ((10, 11), (10, 11), False, "UNKNOWN"),
        ((10, 11), (9, 11), True, "ANOMALY"),
        ((10, 11), (10, 12), True, "VIOLATION"),
        ((10, 11), (10, 11), True, "PASS"),
    ],
)
def test_transition_specification(baseline, observed, authoritative, expected):
    assert transition_oracle(baseline, observed, authoritative=authoritative) == expected


@pytest.mark.parametrize(
    "changed",
    [
        {"scope_complete": False},
        {"search_complete": False},
        {"truncated": True},
        {"unresolved": 1},
        {"blocked": 1},
        {"uncertain": 1},
        {"cleanup_gaps": 1},
    ],
)
def test_completion_specification_never_calls_partial_clean(changed):
    facts = dict(
        scope_complete=True,
        search_complete=True,
        truncated=False,
        unresolved=0,
        blocked=0,
        uncertain=0,
        cleanup_gaps=0,
    )
    assert completion_oracle(**facts) == "COMPLETE"
    assert completion_oracle(**(facts | changed)) == "PARTIAL"


def test_oracle_rejects_unbounded_graph():
    with pytest.raises(ValueError, match="bounds"):
        graph_oracle(tuple(Edge(str(i), str(i + 1)) for i in range(8)))
