"""TargetActionPolicy enforcement: per-target action limits.

Runs after whole scope and ROE pass. Every matched selector policy must be
satisfied: denials win outright, overlapping allow lists intersect. An
explicit empty allow list allows nothing for matched targets; a null allow
list adds no extra restriction for that rule. No matched rule keeps the
existing meaning (no extra check). Matching never grants permission.
"""

from __future__ import annotations

from collections.abc import Sequence

from redteam.control.policy import declared_namespaces, declared_operation_keys
from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import TargetActionPolicy
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.target_selector import selector_matches_target


def check_target_action_policies(
    proposal: ActionProposal,
    policies: Sequence[TargetActionPolicy],
    *,
    selected: ToolDescriptor | None = None,
    has_selected_tool: bool = False,
) -> tuple[bool, list[str], Severity | None]:
    """Evaluate the proposal against matched target action policies.

    Returns (allowed, reasons, severity). Reasons are safe operator strings
    (policy names plus the canonical action key only). Direct selected-tool
    calls match explicit keys against the provider-declared keys (same
    meaning as the policy stage); an undeclared descriptor skips key
    checks here so the policy stage can escalate to a human instead.
    """
    action_key = proposal.capability + "." + proposal.operation
    if has_selected_tool:
        namespaces = declared_namespaces(selected)
        operation_keys = declared_operation_keys(selected)
    else:
        namespaces = None
        operation_keys = {action_key}
    matched = [p for p in policies if selector_matches_target(p.selector, proposal.target)]
    if not matched:
        return True, [], None
    for policy in matched:
        denied = policy.denied_action_keys or []
        denied_entry = next(
            (
                entry
                for entry in denied
                if (operation_keys is not None and entry in operation_keys)
                or (namespaces is not None and entry in namespaces)
            ),
            None,
        )
        if denied_entry is not None:
            label = policy.name or "unnamed"
            return False, [
                "target policy '" + label + "' denies operation '" + denied_entry + "'",
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
            # Explicit empty allow-list keeps its existing contract: it
            # allows nothing for matched targets (plan §5 wording on empty
            # lists needs a root-owned doc correction; code preserves it).
            return False, [
                "target policy '" + _label + "' allows no actions for this target",
            ], Severity.BLOCK_ACTION
    if namespaces is None and operation_keys is None:
        return True, [], None
    for _label, allow in restricted:
        if not any(
            (operation_keys is not None and entry in operation_keys)
            or (namespaces is not None and entry in namespaces)
            for entry in allow
        ):
            return False, [
                "operation '" + action_key + "' is not in the target policy '"
                + _label + "' allowed list",
            ], Severity.BLOCK_ACTION
    return True, [], None
