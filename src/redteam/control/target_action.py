"""TargetActionPolicy enforcement: per-target action limits.

Runs after whole scope and ROE pass. Every matched selector policy must be
satisfied: denials win outright, overlapping allow lists intersect. An
explicit empty allow list allows nothing for matched targets; a null allow
list adds no extra restriction for that rule. No matched rule keeps the
existing meaning (no extra check). Matching never grants permission.
"""

from __future__ import annotations

from collections.abc import Sequence

from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import TargetActionPolicy
from redteam.core.target_selector import selector_matches_target


def check_target_action_policies(
    proposal: ActionProposal,
    policies: Sequence[TargetActionPolicy],
) -> tuple[bool, list[str], Severity | None]:
    """Evaluate the proposal against matched target action policies.

    Returns (allowed, reasons, severity). Reasons are safe operator strings
    (policy names plus the canonical action key only).
    """
    action_key = proposal.capability + "." + proposal.operation
    matched = [p for p in policies if selector_matches_target(p.selector, proposal.target)]
    if not matched:
        return True, [], None
    for policy in matched:
        denied = policy.denied_action_keys or []
        if action_key in denied:
            label = policy.name or "unnamed"
            return False, [
                "target policy '" + label + "' denies operation '" + action_key + "'",
            ], Severity.BLOCK_ACTION
    restricted: list[tuple[str, list[str]]] = []
    for policy in matched:
        allow = policy.allowed_action_keys
        if allow is None:
            continue
        restricted.append((policy.name or "unnamed", list(allow)))
    if not restricted:
        return True, [], None
    for _label, allow in restricted:
        if len(allow) == 0:
            return False, [
                "target policy '" + _label + "' allows no actions for this target",
            ], Severity.BLOCK_ACTION
    for _label, allow in restricted:
        if action_key not in allow:
            return False, [
                "operation '" + action_key + "' is not in the target policy '"
                + _label + "' allowed list",
            ], Severity.BLOCK_ACTION
    return True, [], None
