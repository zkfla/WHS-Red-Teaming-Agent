"""Rate limiting per engagement.

Simple fixed-window counter — enough to prove the pipeline stage
exists and is enforced. TODO(control-team): sliding window, per-agent
and per-capability limits, distributed limiter for multi-process runs.
"""

from __future__ import annotations

import time

from redteam.core.contracts.engagement import RateLimitPolicy


class RateLimiter:
    def __init__(self, policy: RateLimitPolicy) -> None:
        self._policy = policy
        self._window_start = time.monotonic()
        self._count = 0

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= 60:
            self._window_start = now
            self._count = 0
        if self._count >= self._policy.max_actions_per_minute:
            return False
        self._count += 1
        return True

    @property
    def remaining(self) -> int:
        return max(self._policy.max_actions_per_minute - self._count, 0)
