"""Check the existing engagement clock before starting another model request."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.execution_time import check_execution_time


def reasoning_time_allowed(engagement, runtime, *, now=None) -> bool:
    if engagement is None:
        return True
    now = now or datetime.now(timezone.utc)
    allowed, _ = check_execution_time(engagement, now=now)
    if not allowed:
        return False
    duration = engagement.execution_budget.max_duration_seconds
    if duration is None:
        return True
    started = runtime.started_at
    if started is None:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() < duration
