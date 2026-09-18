"""Execution budget tracking per engagement.

Enforces every limit ``ExecutionBudget`` carries:

  - ``max_actions``          — total actions allowed to execute
  - ``max_tool_calls``       — total tool invocations (in this framework
    one allowed action == one ToolInvocation, so it's counted off the
    same consumed count; the tighter of the two limits wins)
  - ``max_duration_seconds`` — wall-clock budget measured from the first
    capacity check (the run's first gated action)

``now`` is injectable so tests drive the duration limit deterministically
instead of sleeping.

TODO(control-team): if the 1-action-per-tool-call assumption ever breaks
(one action fanning out to several tool calls), ``consume`` needs a
separate tool-call count fed back from the Capability layer.
"""

from __future__ import annotations

import time

from redteam.core.contracts.engagement import ExecutionBudget


class BudgetTracker:
    def __init__(self, budget: ExecutionBudget) -> None:
        self._budget = budget
        self._consumed = 0
        self._started_at: float | None = None

    def has_capacity(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self._started_at is None:
            self._started_at = now

        if self._consumed >= self._budget.max_actions:
            return False
        max_tool_calls = self._budget.max_tool_calls
        if max_tool_calls is not None and self._consumed >= max_tool_calls:
            return False
        if self._budget.max_duration_seconds is not None:
            if now - self._started_at >= self._budget.max_duration_seconds:
                return False
        return True

    def consume(self) -> None:
        self._consumed += 1

    @property
    def remaining(self) -> int:
        """Remaining actions under the tighter of the action/tool-call caps."""
        cap = self._budget.max_actions
        if self._budget.max_tool_calls is not None:
            cap = min(cap, self._budget.max_tool_calls)
        return max(cap - self._consumed, 0)
