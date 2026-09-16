"""ROE (Rules of Engagement) stage — runs after Scope, before Policy, in
``DefaultControlPlane.evaluate`` (instruction.md section 9 pipeline).

Return ``(False, [reasons])`` to DENY the action, ``(True, [])`` to let
it continue down the pipeline.

Implemented (control-team, track-a):
  - denied capabilities / operations       -> DENY (deny wins outright)
  - allowed capabilities / operations      -> restrictive allow-list:
    when any rule configures one, an action not in it is DENIED
    (default-deny is safer for a red-team gate)

TODO(control-team, track-a) — remaining ``RuleOfEngagement`` fields:
  - availability_impact_allowed
  - time windows            (start_at / end_at / allowed_time_windows)
  - prohibited_techniques
  - max_parallel_actions    (needs in-flight action count — coordinate
                             with rate_limit/budget stages)
"""

from __future__ import annotations

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement


def check_roe(action: ActionProposal, engagement: Engagement) -> tuple[bool, list[str]]:
    """Evaluate the proposal against the engagement's Rules of Engagement.

    Deny-lists are absolute; allow-lists are restrictive only when
    configured. See the module docstring for the fields still on the
    TODO list.
    """
    rules = engagement.rules_of_engagement
    if not rules:
        # No ROE configured — nothing to enforce at this stage. Scope
        # (the previous stage) already provides target default-deny.
        return True, []

    # 1. Explicit denials win outright, regardless of any allow-list.
    for rule in rules:
        if action.capability in rule.denied_capabilities:
            return False, [f"capability '{action.capability}' denied by ROE rule '{rule.name}'"]
        if action.operation in rule.denied_operations:
            return False, [f"operation '{action.operation}' denied by ROE rule '{rule.name}'"]

    # 2. Allow-lists are restrictive *when configured*: if any rule names
    #    allowed capabilities/operations, the action must be in the
    #    combined allow-list, else it is denied by default.
    allowed_caps = {cap for rule in rules for cap in rule.allowed_capabilities}
    if allowed_caps and action.capability not in allowed_caps:
        return False, [
            f"capability '{action.capability}' is not in the ROE allowed-capability list"
        ]

    allowed_ops = {op for rule in rules for op in rule.allowed_operations}
    if allowed_ops and action.operation not in allowed_ops:
        return False, [
            f"operation '{action.operation}' is not in the ROE allowed-operation list"
        ]

    return True, []
