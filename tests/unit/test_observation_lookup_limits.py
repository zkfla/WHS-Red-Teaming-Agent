"""Admission caps apply to a derived index, never truncate canonical observations."""

import pytest

from redteam_core.knowledge import apply_observations_atomic, lookup, refresh_freshness
from redteam_core.reasoning.focused_context import (
    project_focused_context_retrieval,
    project_focused_decision_context,
)
from tests.performance.workloads import NOW, knowledge_input
from tests.unit.test_focused_context import fixture
from tests.unit.test_priority import pool, select


def test_entry_limit_rejects_the_index_without_returning_partial_data():
    _, _, observations = knowledge_input(10)
    consumed = []

    def rows():
        for row in observations:
            consumed.append(row.observation_id)
            yield row

    assert lookup.index_observations(rows(), max_entries=2) is None
    assert len(consumed) == 3  # One lookahead proves capacity exhaustion.
    assert lookup.index_observations((), max_entries=0) == {}


def test_reference_byte_limit_is_exact_and_independent_of_payload_size():
    _, _, observations = knowledge_input(10)
    row = observations[0]
    size = len(row.observation_id.encode("utf-8")) + len(row.requirement_id.encode("utf-8"))
    assert lookup.index_observations((row,), max_reference_bytes=size - 1) is None
    assert lookup.index_observations((row,), max_reference_bytes=size) == {
        row.requirement_id: [row]
    }


@pytest.mark.parametrize(
    "parameter,value",
    [
        ("max_entries", 100_001),
        ("max_reference_bytes", 8_388_609),
    ],
)
def test_caller_cannot_expand_the_index_past_its_hard_ceiling(parameter, value):
    with pytest.raises(ValueError):
        lookup.index_observations((), **{parameter: value})


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
@pytest.mark.parametrize("parameter", ["max_entries", "max_reference_bytes"])
def test_invalid_cap_does_not_consume_input(parameter, value):
    def rows():
        raise AssertionError("invalid bounds reached input")
        yield

    with pytest.raises(ValueError):
        lookup.index_observations(rows(), **{parameter: value})


@pytest.mark.parametrize("cap", ["entries", "reference_bytes"])
def test_commit_and_freshness_fall_back_to_complete_scan(monkeypatch, cap):
    name = "MAX_OBSERVATION_INDEX_" + cap.upper()
    monkeypatch.setattr(lookup, name, 0)
    outputs = []
    for mode in ("full_scan", "indexed"):
        session, cat, observations = knowledge_input(20)
        committed = apply_observations_atomic(
            session,
            observations,
            catalog=cat,
            evaluation_time=NOW,
            expected_revision=0,
            lookup_mode=mode,
        )
        refreshed = refresh_freshness(
            session,
            evaluation_time=NOW,
            expected_revision=session.knowledge.revision,
            lookup_mode=mode,
        )
        assert len(session.investigation.observations) == 20
        assert len(session.knowledge.slots) == 2
        assert all(row.status.value == "CONSISTENT" for row in session.knowledge.slots.values())
        outputs.append((session, committed, refreshed))
    assert outputs[0] == outputs[1]


def test_context_and_retrieval_preserve_complete_bundles_on_index_overflow(monkeypatch):
    session, candidate, _, secret = fixture()
    kwargs = {"active_candidate_ids": (candidate.candidate_id,)}
    retrieval_ref = f"candidate:{candidate.candidate_id}:observations"
    expected = project_focused_decision_context(session, **kwargs)
    expected_retrieval = project_focused_context_retrieval(session, retrieval_ref=retrieval_ref)
    monkeypatch.setattr(lookup, "MAX_OBSERVATION_INDEX_ENTRIES", 0)
    actual = project_focused_decision_context(session, **kwargs)
    assert actual == expected and actual.complete_bundles == 1
    assert secret not in actual.content
    assert (
        project_focused_context_retrieval(session, retrieval_ref=retrieval_ref)
        == expected_retrieval
    )


def test_priority_facts_scores_and_evidence_hashes_survive_index_overflow(monkeypatch):
    session, runtime, registry = pool(1)
    observed_session, candidate, _, _ = fixture()
    from redteam_core.investigation.coverage import reconcile_candidate_state

    reconcile_candidate_state(observed_session, (candidate,))
    expected = select(observed_session, runtime, registry)
    before = observed_session.model_dump(mode="json")
    monkeypatch.setattr(lookup, "MAX_OBSERVATION_INDEX_ENTRIES", 0)
    assert select(observed_session, runtime, registry) == expected
    assert observed_session.model_dump(mode="json") == before
