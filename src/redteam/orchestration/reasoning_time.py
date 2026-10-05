"""Check the existing engagement clock before starting another model request."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from redteam.core.execution_time import check_execution_time


def reasoning_time_failure(
    engagement, runtime, *, now=None
) -> tuple[str, Literal["budget_exhausted"] | None] | None:
    """Typed cause split. None means another model request is allowed."""

    if engagement is None:
        return None
    now = now or datetime.now(timezone.utc)
    allowed, _ = check_execution_time(engagement, now=now)
    if not allowed:
        return ("reasoning_roe_window_closed", None)
    duration = engagement.execution_budget.max_duration_seconds
    if duration is None:
        return None
    started = runtime.started_at
    if started is None:
        return ("reasoning_start_time_missing", None)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (now - started).total_seconds() >= duration:
        return ("reasoning_duration_exhausted", "budget_exhausted")
    return None


def reasoning_time_allowed(engagement, runtime, *, now=None) -> bool:
    return reasoning_time_failure(engagement, runtime, now=now) is None
