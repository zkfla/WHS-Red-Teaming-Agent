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
  - ``availability_impact_allowed`` — enforced against the agent-declared
    ``ActionProposal.availability_impact`` signal (added for this; see its
    docstring). This is real, agent-declared intent, never inferred from
    ``operation``'s name.
  - ``max_parallel_actions`` — enforced against ``in_flight_actions``, a
    live concurrency count the caller (``DefaultControlPlane.evaluate``)
    supplies via ``ControlContext.metadata["in_flight_actions"]``
    (Orchestration owns the Work Ledger this comes from; Control never
    imports it). A rule that configures this limit without the caller
    supplying the signal fails **closed**, not open — see
    ``_check_max_parallel_actions``.

Still genuinely unsupported (do NOT fake it):
  - ``prohibited_techniques`` — no technique taxonomy/identity exists
    anywhere in this codebase yet (not on ``ActionProposal``, not on
    ``ToolDescriptor``). Guessing a "technique" from ``operation``'s name
    (e.g. ``auth_attempt`` -> ``brute_force``) would be exactly the kind
    of silent, wrong inference this stage must not do. A rule that
    configures this field is therefore rejected explicitly — see
    ``_check_prohibited_techniques`` — rather than silently ignored
    (fail-open) or asked to guess (fabricated).
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement, RuleOfEngagement


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


class UnsupportedRoePolicyError(ValueError):
    """A configured ROE field has no enforcement path yet.

    Raised (never silently swallowed into an ALLOW) so an operator who
    configures ``prohibited_techniques`` gets a loud, explicit failure
    instead of a false sense of safety. See the module docstring.
    """


def check_roe(
    action: ActionProposal,
    engagement: Engagement,
    *,
    now: datetime | None = None,
    in_flight_actions: int | None = None,
) -> tuple[bool, list[str], Severity | None]:
    """Evaluate the proposal against the engagement's Rules of Engagement.

    Returns ``(allowed, reasons, severity)``. ``now`` (default: current
    UTC time) is the clock the time-window rules are evaluated against —
    injectable for tests. ``in_flight_actions`` is the number of *other*
    actions for this run currently leased/running, supplied by the caller
    (Orchestration owns that count) — required only when a rule sets
    ``max_parallel_actions``; see ``_check_max_parallel_actions``.
    Capabilities match on the ``capability`` namespace; operations match
    on the ``<capability>.<operation>`` canonical action key. Deny-lists
    are absolute; allow-lists (capabilities/operations/time-windows) are
    restrictive only when configured.

    Severity hints (see ``redteam.control.severity``):
      - a whole capability being off-limits -> ``REPLAN_GOAL``
      - a single operation being off-limits -> ``BLOCK_ACTION``
      - outside the allowed time window      -> ``ABORT_RUN`` (no action
        will pass until the clock changes)
      - too many actions already in flight   -> ``BLOCK_ACTION`` (transient)

    Raises ``UnsupportedRoePolicyError`` if any rule sets
    ``prohibited_techniques`` — see the module docstring.
    """
    rules = engagement.rules_of_engagement
    if not rules:
        # No ROE configured — nothing to enforce at this stage. Scope
        # (the previous stage) already provides target default-deny.
        return True, [], None

    for rule in rules:
        if rule.prohibited_techniques:
            raise UnsupportedRoePolicyError(
                f"ROE rule '{rule.name}' sets prohibited_techniques="
                f"{rule.prohibited_techniques!r}, but no technique taxonomy or "
                f"signal exists on ActionProposal yet — this cannot be enforced "
                f"safely. Remove this field from the rule until support exists."
            )

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
        if not rule.availability_impact_allowed and action.availability_impact:
            reason = (
                f"action declares availability_impact=True, but ROE rule "
                f"'{rule.name}' has availability_impact_allowed=False"
            )
            return False, [reason], Severity.BLOCK_ACTION
        if rule.max_parallel_actions is not None:
            ok, parallel_reason = _check_max_parallel_actions(rule, in_flight_actions)
            if not ok:
                assert parallel_reason is not None
                return False, [parallel_reason], Severity.BLOCK_ACTION

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


def _check_max_parallel_actions(
    rule: RuleOfEngagement, in_flight_actions: int | None
) -> tuple[bool, str | None]:
    """``max_parallel_actions`` is a runtime concurrency limit, not
    something derivable from ``ActionProposal`` alone — the caller must
    supply the current in-flight count. A rule that configures this limit
    without that signal fails **closed**: silently treating it as
    unlimited (fail-open) would defeat the operator's explicit intent.
    """
    if in_flight_actions is None:
        return False, (
            f"ROE rule '{rule.name}' sets max_parallel_actions="
            f"{rule.max_parallel_actions}, but no in-flight-action count was "
            f"supplied to check_roe() — failing closed rather than silently "
            f"treating the limit as unenforced"
        )
    assert rule.max_parallel_actions is not None
    if in_flight_actions >= rule.max_parallel_actions:
        return False, (
            f"{in_flight_actions} action(s) already in flight for this run "
            f">= ROE rule '{rule.name}' max_parallel_actions="
            f"{rule.max_parallel_actions}"
        )
    return True, None
