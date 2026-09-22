"""Execution budget tracking per engagement.

TODO(control-team): track ``max_tool_calls`` and ``max_duration_seconds``
separately from ``max_actions`` (only the action count is enforced now).
"""

from __future__ import annotations

from redteam.core.contracts.engagement import ExecutionBudget


class BudgetTracker:
    def __init__(self, budget: ExecutionBudget) -> None:
        self._budget = budget
        self._consumed = 0

    def has_capacity(self) -> bool:
        return self._consumed < self._budget.max_actions

    def consume(self) -> None:
        self._consumed += 1

    @property
    def remaining(self) -> int:
        return max(self._budget.max_actions - self._consumed, 0)
