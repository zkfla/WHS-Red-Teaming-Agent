"""RateLimiter tests (control-team, track-a): true sliding window,
driven by an injected clock instead of sleeping."""

from __future__ import annotations

from redteam.control.rate_limit import RateLimiter
from redteam.core.contracts.engagement import RateLimitPolicy


def _limiter(per_minute: int) -> RateLimiter:
    return RateLimiter(RateLimitPolicy(max_actions_per_minute=per_minute))


def test_blocks_over_limit_within_window() -> None:
    rl = _limiter(2)
    assert rl.allow(now=0.0) is True
    assert rl.allow(now=1.0) is True
    assert rl.allow(now=2.0) is False  # 2 actions already in the 60s window
    assert rl.remaining == 0


def test_window_slides_not_fixed_bucket() -> None:
    rl = _limiter(2)
    assert rl.allow(now=0.0) is True
    assert rl.allow(now=30.0) is True
    assert rl.allow(now=31.0) is False  # both still in window
    # At now=61, the action at t=0 has aged out (cutoff=1), one slot frees up.
    assert rl.allow(now=61.0) is True


def test_remaining_reflects_current_window() -> None:
    rl = _limiter(3)
    rl.allow(now=0.0)
    rl.allow(now=1.0)
    assert rl.remaining == 1
