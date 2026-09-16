"""ROE (Rules of Engagement) stage — runs after Scope, before Policy, in
``DefaultControlPlane.evaluate`` (instruction.md section 9 pipeline).

Return ``(False, [reasons])`` to DENY the action, ``(True, [])`` to let
it continue down the pipeline.

Naming convention (PM-confirmed, ``docs/contracts/actions.md`` "Action
Naming Convention"):
  - ``capability`` is a namespace (``recon`` / ``web`` / ``mobile`` /
    ``analysis``) — matched exactly against ``allowed/denied_capabilities``.
  - ``operation`` is a canonical ``lower_snake_case`` action; ROE
    operation policy is evaluated against the **canonical action key**
    ``<capability>.<operation>`` (e.g. ``recon.port_scan``), matched
    exactly against ``allowed/denied_operations``.
  - No wildcard / namespace-prefix matching.

Implemented (control-team, track-a):
  - denied capabilities / operations   -> DENY (deny wins outright)
  - allowed capabilities / operations  -> restrictive allow-list
  - time windows: ``start_at`` / ``end_at`` (absolute) and
    ``allowed_time_windows`` (time-of-day, restrictive when configured)

BLOCKED — needs a signal not present in ``ActionProposal`` (candidate
for a follow-up PM/contract decision, do NOT fake it):
  - ``availability_impact_allowed`` — no "does this action impact
    availability?" flag on the proposal.
  - ``prohibited_techniques`` — no "technique" identity on the proposal.
  - ``max_parallel_actions`` — needs in-flight action count (coordinate
    with rate_limit/budget stages).
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime (e.g. from YAML without a tz) as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_window(window: str) -> tuple[time, time] | None:
    """Parse a ``"HH:MM-HH:MM"`` time window; ``None`` if malformed."""
    try:
        start_s, end_s = window.split("-", 1)
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None
    return start, end


def _time_in_window(now_t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_t <= end
    # Overnight window, e.g. "22:00-04:00".
    return now_t >= start or now_t <= end


def check_roe(
    action: ActionProposal,
    engagement: Engagement,
    *,
    now: datetime | None = None,
) -> tuple[bool, list[str], Severity | None]:
    """Evaluate the proposal against the engagement's Rules of Engagement.

    Returns ``(allowed, reasons, severity)``. ``now`` (default: current
    UTC time) is the clock the time-window rules are evaluated against —
    injectable for tests. Capabilities match on the ``capability``
    namespace; operations match on the ``<capability>.<operation>``
    canonical action key. Deny-lists are absolute; allow-lists
    (capabilities/operations/time-windows) are restrictive only when
    configured.

    Severity hints (see ``redteam.control.severity``):
      - a whole capability being off-limits -> ``REPLAN_GOAL``
      - a single operation being off-limits -> ``BLOCK_ACTION``
      - outside the allowed time window      -> ``ABORT_RUN`` (no action
        will pass until the clock changes)
    """
    rules = engagement.rules_of_engagement
    if not rules:
        # No ROE configured — nothing to enforce at this stage. Scope
        # (the previous stage) already provides target default-deny.
        return True, [], None

    now = now or datetime.now(timezone.utc)
    action_key = f"{action.capability}.{action.operation}"

    # 1. Explicit denials win outright, regardless of any allow-list.
    for rule in rules:
        if action.capability in rule.denied_capabilities:
            reason = f"capability '{action.capability}' denied by ROE rule '{rule.name}'"
            return False, [reason], Severity.REPLAN_GOAL
        if action_key in rule.denied_operations:
            reason = f"operation '{action_key}' denied by ROE rule '{rule.name}'"
            return False, [reason], Severity.BLOCK_ACTION
        if rule.start_at is not None and now < _as_utc(rule.start_at):
            reason = (
                f"ROE rule '{rule.name}' window has not started "
                f"(starts {_as_utc(rule.start_at).isoformat()})"
            )
            return False, [reason], Severity.ABORT_RUN
        if rule.end_at is not None and now > _as_utc(rule.end_at):
            reason = (
                f"ROE rule '{rule.name}' window has ended "
                f"(ended {_as_utc(rule.end_at).isoformat()})"
            )
            return False, [reason], Severity.ABORT_RUN

    # 2. Time-of-day windows are restrictive when configured: if any rule
    #    lists windows, ``now`` must fall inside at least one.
    windows = [w for rule in rules for w in rule.allowed_time_windows]
    if windows:
        parsed = [pw for w in windows if (pw := _parse_window(w)) is not None]
        if not any(_time_in_window(now.time(), start, end) for start, end in parsed):
            reason = (
                f"current time {now.time().strftime('%H:%M')} UTC is outside the "
                f"ROE allowed time windows {windows}"
            )
            return False, [reason], Severity.ABORT_RUN

    # 3. Capability / operation allow-lists (restrictive when configured).
    allowed_caps = {cap for rule in rules for cap in rule.allowed_capabilities}
    if allowed_caps and action.capability not in allowed_caps:
        reason = f"capability '{action.capability}' is not in the ROE allowed-capability list"
        return False, [reason], Severity.REPLAN_GOAL

    allowed_ops = {op for rule in rules for op in rule.allowed_operations}
    if allowed_ops and action_key not in allowed_ops:
        reason = f"operation '{action_key}' is not in the ROE allowed-operation list"
        return False, [reason], Severity.BLOCK_ACTION

    return True, [], None
