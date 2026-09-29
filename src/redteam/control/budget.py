"""Execution budget tracking per engagement.

Enforces every limit ``ExecutionBudget`` carries:

  - ``max_actions``          — total actions allowed to execute
  - ``max_tool_calls``       — total tool invocations. One admitted action
    charges one action *and* one tool call; a bounded retry (see
    ``orchestration.nodes.execute``) charges an extra tool call through
    ``consume_tool_calls`` so a retry cannot silently exceed the tighter of
    the two caps.
  - ``max_duration_seconds`` — wall-clock budget measured from the first
    capacity check (the run's first gated action)

``now`` is injectable so tests drive the duration limit deterministically
instead of sleeping.

Process restart: ``rehydrate`` restores the consumed counters a previous
process durably recorded, *monotonically* and clamped to the engagement
caps. It can only ever reduce remaining capacity — a stale or hostile
caller cannot use it to hand back budget. The duration cap is restored the
same way: ``time.monotonic`` is process-local and no durable start instant
is recorded, so the resume caller supplies ``elapsed_duration_seconds``
(how much wall clock the run already consumed) and the tracker anchors its
clock so that time is never handed back. When a duration cap is configured
but no elapsed time is supplied, ``rehydrate`` fails closed — the duration
is treated as already spent rather than silently restarting the full
window on resume.
"""

from __future__ import annotations

import math
import time

from redteam.core.contracts.engagement import ExecutionBudget


def _validated_elapsed_duration(elapsed: float, *, cap: int | None) -> float:
    """Validate and bound a rehydrated duration, rejecting corrupt input.

    ``NaN`` would make the elapsed comparison always false (silently
    *granting* the full duration), so non-finite values are refused. When a
    duration cap exists the value is clamped to it: past the cap the budget
    is exhausted either way, so a huge or corrupt value can never widen the
    window.
    """
    if not math.isfinite(elapsed):
        raise ValueError(
            f"elapsed_duration_seconds must be finite, got {elapsed!r}"
        )
    if elapsed < 0:
        raise ValueError(
            f"elapsed_duration_seconds must be non-negative, got {elapsed}"
        )
    if cap is None:
        return elapsed
    return min(elapsed, float(cap))


class BudgetTracker:
    def __init__(self, budget: ExecutionBudget) -> None:
        self._budget = budget
        self._consumed = 0
        self._tool_calls_consumed = 0
        self._started_at: float | None = None

    @property
    def _tool_calls_cap(self) -> int:
        """The effective tool-call cap: ``max_tool_calls`` when set, else
        ``max_actions`` (one admitted action is one tool call)."""
        if self._budget.max_tool_calls is not None:
            return self._budget.max_tool_calls
        return self._budget.max_actions

    def has_capacity(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self._started_at is None:
            self._started_at = now

        if self._consumed >= self._budget.max_actions:
            return False
        if self._tool_calls_consumed >= self._tool_calls_cap:
            return False
        if self._budget.max_duration_seconds is not None:
            if now - self._started_at >= self._budget.max_duration_seconds:
                return False
        return True

    def consume(self) -> None:
        """Charge one admitted action (which is also one tool call)."""
        self._consumed = min(self._consumed + 1, self._budget.max_actions)
        self._tool_calls_consumed = min(self._tool_calls_consumed + 1, self._tool_calls_cap)

    def consume_tool_calls(self, count: int = 1) -> None:
        """Charge ``count`` extra tool calls without admitting an action.

        Used for bounded retries, which re-invoke an already-admitted
        action's tool. Clamped to the tool-call cap so the counter can
        never be inflated past it (the next ``has_capacity`` then reports
        exhausted).
        """
        if count < 0:
            raise ValueError(f"tool-call count must be non-negative, got {count}")
        self._tool_calls_consumed = min(
            self._tool_calls_consumed + count, self._tool_calls_cap
        )

    def rehydrate(
        self,
        *,
        actions_consumed: int,
        tool_calls_consumed: int | None = None,
        elapsed_duration_seconds: float | None = None,
        now: float | None = None,
    ) -> None:
        """Restore consumed counters for an already-running engagement.

        Monotonic and capped: each counter is raised to at most the
        engagement's own limit and never lowered below what this tracker
        already recorded. A counter exceeding its cap simply means the
        budget is exhausted — it can never *widen* the budget. ``None``
        for ``tool_calls_consumed`` charges one tool call per consumed
        action (the no-retry case). Tool calls are never counted below
        actions, since every admitted action is at least one tool call.

        ``elapsed_duration_seconds`` restores the wall-clock time an
        already-running engagement has burned through, so a restart does not
        silently reset ``max_duration_seconds``. It must be finite and
        non-negative and is clamped to the engagement's own duration cap;
        corrupt/negative/non-finite input raises rather than widening the
        window. When a duration cap is configured but no elapsed time is
        supplied, the restore *fails closed*: the whole duration is treated
        as spent, so no further action is admitted until the caller supplies
        the persisted elapsed time. When no duration cap is configured the
        value is validated but otherwise ignored, so behaviour is unchanged.

        The clock is anchored at ``now`` (injectable for tests, defaulting to
        ``time.monotonic``); the anchor only ever moves earlier — counting
        *more* elapsed time — so repeated or partial rehydrates cannot hand
        back time.
        """
        if actions_consumed < 0:
            raise ValueError(f"actions_consumed must be non-negative, got {actions_consumed}")
        actions = max(self._consumed, min(actions_consumed, self._budget.max_actions))

        if tool_calls_consumed is None:
            requested_tool_calls = actions
        else:
            if tool_calls_consumed < 0:
                raise ValueError(
                    f"tool_calls_consumed must be non-negative, got {tool_calls_consumed}"
                )
            requested_tool_calls = tool_calls_consumed
        # Every action implies >= 1 tool call; clamp to the tool-call cap.
        tool_calls = min(max(requested_tool_calls, actions), self._tool_calls_cap)
        tool_calls = max(tool_calls, self._tool_calls_consumed)

        self._consumed = actions
        self._tool_calls_consumed = tool_calls

        max_duration = self._budget.max_duration_seconds
        elapsed = (
            None
            if elapsed_duration_seconds is None
            else _validated_elapsed_duration(
                elapsed_duration_seconds, cap=max_duration
            )
        )
        if max_duration is None:
            # No duration cap: nothing to anchor, existing behaviour.
            return
        anchor = time.monotonic() if now is None else now
        if elapsed is None:
            # Fail closed: no durable elapsed time was supplied, so treat the
            # whole duration as already spent rather than restarting it.
            candidate_start = anchor - max_duration
        else:
            candidate_start = anchor - elapsed
        # Monotonic anchoring: an earlier start means more elapsed time
        # counted, so only ever move it earlier -- never hand time back.
        if self._started_at is None or candidate_start < self._started_at:
            self._started_at = candidate_start

    @property
    def consumed(self) -> int:
        return self._consumed

    @property
    def tool_calls_consumed(self) -> int:
        return self._tool_calls_consumed

    @property
    def remaining(self) -> int:
        """Remaining actions under the tighter of the action/tool-call caps."""
        actions_remaining = max(self._budget.max_actions - self._consumed, 0)
        tool_calls_remaining = max(self._tool_calls_cap - self._tool_calls_consumed, 0)
        return min(actions_remaining, tool_calls_remaining)

    @property
    def tool_calls_remaining(self) -> int:
        return max(self._tool_calls_cap - self._tool_calls_consumed, 0)
