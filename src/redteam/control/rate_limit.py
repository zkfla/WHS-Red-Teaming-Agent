"""Rate limiting per engagement.

Sliding-window limiter: an action is allowed only if fewer than
``max_actions_per_minute`` actions occurred in the trailing 60 seconds
(a true rolling window, not a fixed bucket that resets on the minute).

``now`` is injectable on every method so tests drive time deterministically
instead of sleeping.

``would_allow`` is the non-consuming admission check (a REQUIRE_APPROVAL
decision reports window state without charging the slot twice); ``rehydrate``
conservatively restores window occupancy after a process restart, capped and
monotonic so it can never hand back a fresh window.

``max_concurrent_actions`` is admission-visible through
``concurrency_exceeded``: Orchestration already supplies the in-flight
count in ``ControlContext.metadata["in_flight_actions"]`` (the same signal
ROE ``max_parallel_actions`` uses), so Control can report whether the bound
would be exceeded. The *execution* bound itself is enforced at the tool
call site (``orchestration.context.RuntimeDeps.tool_semaphore``), because
the Control Plane owns admission, not execution, and a hard admission
denial would defeat the bounded fan-out path that intentionally admits a
batch. When in-flight feedback is absent the bound is not applied.

TODO(control-team): per-agent / per-capability limits, distributed limiter
for multi-process runs.
"""

from __future__ import annotations

import time
from collections import deque

from redteam.core.contracts.engagement import RateLimitPolicy

_WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(self, policy: RateLimitPolicy) -> None:
        self._policy = policy
        self._events: deque[float] = deque()

    def _evict(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def would_allow(self, *, now: float | None = None) -> bool:
        """Whether ``allow()`` would succeed, *without* consuming a slot.

        Used for the pre-approval admission check: a ``REQUIRE_APPROVAL``
        decision must be able to report whether the window is currently
        open without charging the slot for an action that has not been
        admitted yet (the slot is charged exactly once, when the action is
        actually admitted — see ``DefaultControlPlane.evaluate`` /
        ``approval_outcome``).
        """
        now = time.monotonic() if now is None else now
        self._evict(now)
        return len(self._events) < self._policy.max_actions_per_minute

    def allow(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._evict(now)
        if len(self._events) >= self._policy.max_actions_per_minute:
            return False
        self._events.append(now)
        return True

    def rehydrate(self, actions_in_window: int, *, now: float | None = None) -> None:
        """Restore occupancy of the trailing window after a restart.

        The sliding window is intentionally *not* durable (its event
        timestamps are monotonic and process-local), so a fresh process
        rehydrates conservatively: ``actions_in_window`` slots are placed
        at ``now``, i.e. every restored action is treated as having just
        happened and ages out one window from the resume. Monotonic and
        capped at the per-minute limit, so rehydrating can only ever
        reduce remaining capacity, never grant a fresh full window.
        """
        now = time.monotonic() if now is None else now
        if actions_in_window < 0:
            raise ValueError(
                f"actions_in_window must be non-negative, got {actions_in_window}"
            )
        count = min(actions_in_window, self._policy.max_actions_per_minute)
        count = max(count, len(self._events))
        self._events.clear()
        self._events.extend([now] * count)

    @property
    def in_window(self) -> int:
        """Actions currently occupying the window (after the last eviction)."""
        return len(self._events)

    @property
    def remaining(self) -> int:
        """Slots left in the window as of the last ``allow()``/eviction.
        The Control Plane reads this right after ``allow()``, so it
        reflects the current window without needing a clock here."""
        return max(self._policy.max_actions_per_minute - len(self._events), 0)

    @property
    def max_concurrent_actions(self) -> int:
        """The engagement's concurrency bound (always >= 1)."""
        return self._policy.max_concurrent_actions

    def concurrency_exceeded(self, in_flight: int | None) -> bool:
        """Whether admitting one more action would exceed the bound.

        ``in_flight`` counts *other* leased/running actions. ``None`` means
        the caller has no in-flight feedback (legacy/serial path) and the
        bound is not applied — preserving behaviour where the Control Plane
        never observed concurrency.
        """
        if in_flight is None:
            return False
        return in_flight >= self.max_concurrent_actions
