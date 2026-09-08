import itertools

import pytest

from redteam_core.reasoning import project_decision_context
from tests.performance.paired import paired_samples
from tests.performance.workloads import knowledge_input, populate_context_candidates


@pytest.mark.parametrize("count", [10, 100, 1000])
def test_context_fixture_has_actual_candidates_and_valid_coverage(count):
    session, _, _ = knowledge_input(10)
    populate_context_candidates(session, count)
    assert len(session.candidate_state.candidates) == count
    assert len(session.candidate_state.coverage_ledger) == count
    result = project_decision_context(session)
    assert len(result.content) <= 12000
    assert result.omitted_candidates > 0 if count == 1000 else True
    assert result == project_decision_context(session)


def test_paired_execution_alternates_and_excludes_warmups():
    calls = []

    def factory(arm):
        def prepare():
            calls.append(arm)
            return lambda: {"canonical": 1}, lambda result: result

        return prepare

    clock = itertools.count(1).__next__
    result = paired_samples(factory(0), factory(1), repetitions=4, warmup=1, clock=clock)
    assert calls == [0, 1, 1, 0, 0, 1, 1, 0, 0, 1]
    assert result["baseline_ns"]["count"] == 4
    assert result["comparison"]["assessment"] == "INCONCLUSIVE"


def test_paired_mismatch_and_operation_failure_propagate():
    def factory(value):
        return lambda: (lambda: value, lambda result: result)

    with pytest.raises(AssertionError, match="canonical mismatch"):
        paired_samples(factory(1), factory(2), repetitions=2, warmup=0)

    def broken():
        raise RuntimeError("operation failed")

    with pytest.raises(RuntimeError, match="operation failed"):
        paired_samples(lambda: (broken, lambda result: result), factory(1), repetitions=2)
