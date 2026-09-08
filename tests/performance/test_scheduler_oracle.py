import pytest

from tests.performance.oracles import scheduler_oracle

PREFIX = (("RESERVE", "a"), ("DISPATCH", "a"), ("CALL", "a"), ("RESULT", "a"), ("COMMIT", "a"))


@pytest.mark.parametrize(
    "cut,remaining,state,calls",
    [
        (0, 1, None, 0),
        (1, 1, None, 0),
        (2, 0, "UNCERTAIN", 0),
        (3, 0, "UNCERTAIN", 1),
        (4, 0, "RESULT", 1),
        (5, 0, "COMMITTED", 1),
    ],
)
def test_crash_matrix(cut, remaining, state, calls):
    result = scheduler_oracle((*PREFIX[:cut], ("RESTART", "")), 1)
    assert result["remaining"] == remaining
    assert result["states"].get("a") == state
    assert result["calls"] == calls
    assert result["visible"] == (frozenset({"a"}) if cut == 5 else frozenset())


def test_receipt_recovery_does_not_recall_and_commit_is_idempotent():
    result = scheduler_oracle((*PREFIX[:4], ("RESTART", ""), ("COMMIT", "a"), ("COMMIT", "a")), 1)
    assert result["calls"] == 1
    assert result["visible"] == {"a"}


@pytest.mark.parametrize("event", [("RESERVE", "b"), ("CALL", "a"), ("RESULT", "a")])
def test_uncertain_dispatch_cannot_reset_budget_or_replay(event):
    with pytest.raises(ValueError):
        scheduler_oracle((*PREFIX[:3], ("RESTART", ""), event), 1)


def test_failed_call_consumes_budget_without_semantic_visibility():
    result = scheduler_oracle((*PREFIX[:3], ("FAILURE", "a"), ("RESTART", "")), 1)
    assert result["remaining"] == 0
    assert not result["visible"]
