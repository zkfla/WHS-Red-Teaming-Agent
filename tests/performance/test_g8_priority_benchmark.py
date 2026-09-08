import pytest

from tests.performance.g8_priority_benchmark import FIXTURES, run_case


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["baseline", "utility"])
async def test_fixed_budget_ground_truth_smoke_preserves_negative_and_conflict(tmp_path, arm):
    result = await run_case(FIXTURES[3], budget=3, arm=arm, directory=tmp_path)
    assert result["status"] == "PASS"
    assert result["incorrect_root_decisions"] == 0
    assert result["correct_root_decisions"] == result["ground_truth_root_count"] == 3
    assert result["conflicting_requirements"] == 1
    assert result["outcome_labels"] == ["PASS", "UNKNOWN_CONFLICT", "VIOLATION"]
    assert result["resolver_calls"] == 2 and result["duplicate_calls"] == 0


@pytest.mark.asyncio
async def test_zero_call_budget_does_not_invent_resolution_or_completion(tmp_path):
    result = await run_case(FIXTURES[0], budget=0, arm="utility", directory=tmp_path)
    assert result["resolver_calls"] == result["correct_root_decisions"] == 0
    assert result["ground_truth_root_count"] == 4
    assert result["first_valid_outcome_ms"] is None


@pytest.mark.asyncio
async def test_actual_invalid_responses_are_known_failures_and_preserve_distinct_attempts(tmp_path):
    result = await run_case(FIXTURES[-1], budget=6, arm="baseline", directory=tmp_path)
    assert result["known_failed_dispatches"] == result["max_durable_root_failures"] == 3
    assert result["uncertain_dispatches"] == 0 and result["duplicate_calls"] == 2
    assert result["incorrect_root_decisions"] == 0
