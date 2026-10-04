"""Execution time guard: pure core helper for ROE time windows.

Team boundary: orchestration must not import control implementations.
Root consumes this pure helper for model-call and deadline guards, and
control.roe consumes the same helper so both sides share one meaning.

Scope is time only: start_at, end_at and allowed_time_windows.
Full permission judgement (capability and operation allow and deny lists,
availability impact, max parallel actions, prohibited techniques) stays in
control.roe.check_roe. Duration budget (runtime.started_at plus
execution_budget.max_duration_seconds) is owned by root, not here.

Meaning matches the pre-existing roe time checks: naive datetimes are
treated as UTC, end_at denies when now is past the end, start_at denies
when now is before the start, allowed_time_windows is restrictive when
configured, overnight windows wrap, and malformed window strings are
ignored.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from redteam.core.contracts.engagement import Engagement


def as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime (for example from YAML without tz) as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def parse_window(window: str) -> tuple[time, time] | None:
    """Parse a HH:MM-HH:MM time window; None if malformed."""
    try:
        start_s, end_s = window.split("-", 1)
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None
    return start, end


def time_in_window(now_t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_t <= end
    return now_t >= start or now_t <= end


def check_execution_time(
    engagement: Engagement,
    now: datetime | None = None,
) -> tuple[bool, list[str]]:
    """Time-only gate for an engagement.

    Returns (allowed, reasons). True means the current instant is inside
    all configured start and end bounds and time-of-day windows. Reasons
    are safe operator strings (rule names plus clock values only).
    """
    rules = engagement.rules_of_engagement
    if not rules:
        return True, []
    now = now or datetime.now(timezone.utc)
    for rule in rules:
        if rule.start_at is not None and now < as_utc(rule.start_at):
            return False, [
                "ROE rule '" + rule.name + "' window has not started "
                + "(starts " + as_utc(rule.start_at).isoformat() + ")",
            ]
        if rule.end_at is not None and now > as_utc(rule.end_at):
            return False, [
                "ROE rule '" + rule.name + "' window has ended "
                + "(ended " + as_utc(rule.end_at).isoformat() + ")",
            ]
    windows = [w for rule in rules for w in rule.allowed_time_windows]
    if windows:
        parsed = [pw for w in windows if (pw := parse_window(w)) is not None]
        if not any(time_in_window(now.time(), start, end) for start, end in parsed):
            return False, [
                "current time " + now.time().strftime("%H:%M") + " UTC is outside the "
                + "ROE allowed time windows " + str(windows),
            ]
    return True, []
