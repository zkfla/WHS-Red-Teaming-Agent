"""BudgetTracker tests (control-team, track-a): action / tool-call /
duration limits, with an injected clock for the duration case."""

from __future__ import annotations

from redteam.control.budget import BudgetTracker
from redteam.core.contracts.engagement import ExecutionBudget


def test_max_actions_enforced() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=2))
    assert tracker.has_capacity() is True
    tracker.consume()
    tracker.consume()
    assert tracker.has_capacity() is False
    assert tracker.remaining == 0


def test_max_tool_calls_is_the_tighter_cap() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5, max_tool_calls=2))
    assert tracker.remaining == 2  # min(5, 2)
    tracker.consume()
    tracker.consume()
    assert tracker.has_capacity() is False


def test_max_duration_enforced_with_injected_clock() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=100, max_duration_seconds=10))
    assert tracker.has_capacity(now=0.0) is True  # starts the clock at 0
    assert tracker.has_capacity(now=9.0) is True
    assert tracker.has_capacity(now=10.0) is False  # window elapsed


def test_no_optional_limits_only_actions() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=1))
    assert tracker.has_capacity() is True
    tracker.consume()
    assert tracker.has_capacity() is False
