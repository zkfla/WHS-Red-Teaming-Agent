"""Rate limiting per engagement.

Sliding-window limiter: an action is allowed only if fewer than
``max_actions_per_minute`` actions occurred in the trailing 60 seconds
(a true rolling window, not a fixed bucket that resets on the minute).

``now`` is injectable on every method so tests drive time deterministically
instead of sleeping.

TODO(control-team): per-agent / per-capability limits, ``max_concurrent_actions``
(needs in-flight action start/finish feedback the Control Plane doesn't
observe yet — same gap as ROE ``max_parallel_actions``), distributed
limiter for multi-process runs.
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

    def allow(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._evict(now)
        if len(self._events) >= self._policy.max_actions_per_minute:
            return False
        self._events.append(now)
        return True

    @property
    def remaining(self) -> int:
        """Slots left in the window as of the last ``allow()``/eviction.
        The Control Plane reads this right after ``allow()``, so it
        reflects the current window without needing a clock here."""
        return max(self._policy.max_actions_per_minute - len(self._events), 0)
