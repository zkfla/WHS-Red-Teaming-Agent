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


# --- max_concurrent_actions admission signal ---------------------------


def _concurrency_limiter(limit: int) -> RateLimiter:
    return RateLimiter(
        RateLimitPolicy(max_actions_per_minute=30, max_concurrent_actions=limit)
    )


def test_concurrency_bound_is_exposed() -> None:
    assert _concurrency_limiter(1).max_concurrent_actions == 1
    assert _concurrency_limiter(4).max_concurrent_actions == 4


def test_concurrency_exceeded_only_when_in_flight_known() -> None:
    rl = _concurrency_limiter(2)
    # No feedback (legacy/serial path) -> bound not applied.
    assert rl.concurrency_exceeded(None) is False
    assert rl.concurrency_exceeded(0) is False
    assert rl.concurrency_exceeded(1) is False
    assert rl.concurrency_exceeded(2) is True
    assert rl.concurrency_exceeded(5) is True


def test_concurrency_check_does_not_consume_window() -> None:
    rl = _concurrency_limiter(1)
    assert rl.concurrency_exceeded(1) is True
    # A concurrency denial must not have recorded a rate-limit event.
    assert rl.remaining == 30
