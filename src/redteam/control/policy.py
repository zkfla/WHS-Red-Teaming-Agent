"""Policy Engine — decides ALLOW vs. REQUIRE_APPROVAL for in-scope,
ROE-permitted actions.

Division of responsibility:
  - **Denials** are ROE's job (``redteam.control.roe`` —
    ``denied_capabilities`` / ``denied_operations``). The policy engine
    does not deny; it gates actions behind human approval.
  - This stage decides whether an otherwise-permitted action needs
    approval, from three sources:
      1. ``ApprovalPolicy.require_approval_for`` (operator-listed
         capabilities / canonical action keys), and
      2. **risk-tiered approval** — when the action's risk (resolved from
         the selected tool's own ``risk_level``, see ``redteam.control.risk``) meets
         a threshold, approval is required regardless of the list, and
      3. **command-pattern approval** — when the selected tool's
         provider-declared command fields match an operator-configured
         ``command_approval_rules`` entry. A firing rule always needs a
         human and bypasses auto-approval ceilings.

Matching for (1) (consistent with ROE and ``docs/contracts/actions.md``):
an ``require_approval_for`` entry matches either
  - a ``capability`` namespace (e.g. ``"mobile"`` gates all mobile
    actions), or
  - a canonical action key ``<capability>.<operation>`` (e.g.
    ``"recon.port_scan"`` gates just that action).

For (2), ``risk`` comes from ``ToolDescriptor.risk_level`` via the
read-only ``ToolGateway.list_tools_for_action`` port — Control reads the
registry's value rather than hardcoding one. The threshold is now a
per-engagement ``ApprovalPolicy.risk_threshold`` field; when unset the
Engine default (``control.risk.DEFAULT_APPROVAL_RISK_THRESHOLD``) applies,
so legacy engagements behave exactly as before. ``auto_approval_ceiling`` /
``may_auto_approve`` implement bounded auto-approval while keeping the
legacy ``auto_approve`` flag meaningful.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from redteam.control.risk import (
    DEFAULT_APPROVAL_RISK_THRESHOLD,
    risk_at_or_below,
    risk_meets_threshold,
)
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy, CommandApprovalRule, Engagement
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import PolicyVerdict, RiskLevel
from redteam.core.outbound_text import matches_prohibited_patterns
from redteam.core.target_selector import selector_matches_target


def resolve_risk_threshold(policy: ApprovalPolicy) -> RiskLevel:
    """The effective risk-approval threshold for this engagement.

    An engagement-configured ``risk_threshold`` wins; otherwise the
    Engine-wide default (``HIGH``) applies, so an unconfigured engagement
    keeps exactly the pre-configurable behaviour.
    """
    if policy.risk_threshold is not None:
        return policy.risk_threshold
    return DEFAULT_APPROVAL_RISK_THRESHOLD


def auto_approval_ceiling(policy: ApprovalPolicy) -> RiskLevel | None:
    """The highest risk that may be auto-approved, or ``None`` for no
    auto-approval.

    Precedence: an explicit ``auto_approve_up_to`` wins; otherwise the
    legacy ``auto_approve`` flag maps to ``LOW``. Old YAML still parses,
    but cannot silently bypass HIGH/CRITICAL review.
    """
    if policy.auto_approve_up_to is not None:
        return policy.auto_approve_up_to
    if policy.auto_approve:
        return RiskLevel.LOW
    return None


def may_auto_approve(policy: ApprovalPolicy, risk: RiskLevel | None) -> bool:
    """Whether a REQUIRE_APPROVAL decision may be auto-approved.

    Only a *known* risk at or below the ceiling can be auto-approved.
    An unresolved risk always needs a human decision.
    """
    ceiling = auto_approval_ceiling(policy)
    if ceiling is None:
        return False
    if risk is None:
        return False
    return risk_at_or_below(risk, ceiling)


COMMAND_MATCH_PREFIX = "command:"
#: Matched rule when an explicit user policy cannot be evaluated against
#: the selected tool's provider declarations. Never silently skipped:
#: forces a human decision with config guidance in the reason.
POLICY_SEMANTICS_UNAVAILABLE = "policy_semantics_unavailable"
"""
Default dangerous-command rules (policy-owner baseline, C1).

Applied to selected-tool calls only, and only when the engagement sets
no explicit ``command_approval_rules`` (explicit rules replace this
baseline). Legacy callers without a selected tool keep their existing
behaviour so unconfigured engagements do not change meaning there.
Patterns are literal case-insensitive substrings (same matcher as the
outbound-text gate) matched against the selected tool's
provider-declared ``command_fields`` -- never against tool names, and
never reused from ``ExternalTextPolicy.prohibited_patterns`` (DENY
meaning stays separate from approval meaning).
"""
DEFAULT_COMMAND_APPROVAL_RULES: tuple[CommandApprovalRule, ...] = (
    CommandApprovalRule(
        name="destructive-command",
        patterns=[
            "rm -rf",
            "mkfs",
            "dd if=",
            ":(){",
            "shutdown",
            "poweroff",
            "del /s",
            "rmdir /s",
            "format c:",
        ],
        minimum_risk=RiskLevel.HIGH,
    ),
)


def default_command_approval_rules() -> list[CommandApprovalRule]:
    """A fresh copy of the policy-owner default command rules."""
    return [rule.model_copy(deep=True) for rule in DEFAULT_COMMAND_APPROVAL_RULES]


def declared_namespaces(selected: ToolDescriptor | None) -> set[str] | None:
    """Provider-declared capability namespaces, or ``None`` when undeclared.

    Only actual provider declarations count: the proposal's capability
    string (LLM-written on new direct calls) is never used as a
    namespace here.
    """
    if selected is None:
        return None
    tags = [
        tag for tag in (selected.capability_tags or []) if isinstance(tag, str) and tag
    ]
    if not tags:
        return None
    return set(tags)


def declared_operation_keys(selected: ToolDescriptor | None) -> set[str] | None:
    """Provider-declared canonical ``<namespace>.<operation>`` keys.

    ``None`` when no canonical key can be formed: no operations declared,
    or operations declared without any namespace to pair them with.
    """
    namespaces = declared_namespaces(selected)
    if namespaces is None:
        return None
    operations = [
        operation
        for operation in (selected.operations or [])
        if isinstance(operation, str) and operation
    ]
    if not operations:
        return None
    return {f"{namespace}.{operation}" for namespace in namespaces for operation in operations}


def unavailable_restriction_reasons(
    proposal: ActionProposal,
    engagement: Engagement,
    *,
    selected: ToolDescriptor | None = None,
    has_selected_tool: bool = False,
) -> list[str]:
    """Explicit restrictions with no provider declaration to evaluate.

    Only for selected-tool calls, split by restriction kind: capability
    restrictions need declared namespaces, operation restrictions need
    declared canonical keys. Empty restriction lists mean no additional
    restriction and never report here. A non-empty result must force a
    human decision (``POLICY_SEMANTICS_UNAVAILABLE``), never a silent
    pass: time/availability/parallel gates do not need declarations and
    are enforced elsewhere.
    """
    if not has_selected_tool:
        return []
    namespaces = declared_namespaces(selected)
    operation_keys = declared_operation_keys(selected)
    if namespaces is not None and operation_keys is not None:
        return []
    reasons: list[str] = []
    for rule in engagement.rules_of_engagement or []:
        if (rule.denied_capabilities or rule.allowed_capabilities) and namespaces is None:
            label = rule.name or "unnamed"
            reasons.append(
                f"ROE rule '{label}' sets explicit capability restrictions "
                "that cannot be evaluated against the selected tool's provider "
                "declarations -- declare provider capability tags or relax the rule"
            )
        if (rule.denied_operations or rule.allowed_operations) and operation_keys is None:
            label = rule.name or "unnamed"
            reasons.append(
                f"ROE rule '{label}' sets explicit operation restrictions "
                "that cannot be evaluated against the selected tool's provider "
                "declarations -- declare provider operations with a capability "
                "namespace or relax the rule"
            )
    for policy in engagement.target_action_policies or []:
        try:
            matched = selector_matches_target(policy.selector, proposal.target)
        except Exception:
            matched = False
        if not matched:
            continue
        allow = policy.allowed_action_keys
        denied = list(policy.denied_action_keys or [])
        denied_namespaces = [entry for entry in denied if "." not in entry]
        denied_keys = [entry for entry in denied if "." in entry]
        allow_list = list(allow) if allow is not None else []
        allow_namespaces = [entry for entry in allow_list if "." not in entry]
        allow_keys = [entry for entry in allow_list if "." in entry]
        if (denied_namespaces or allow_namespaces) and namespaces is None:
            label = policy.name or "unnamed"
            reasons.append(
                f"target policy '{label}' sets explicit capability restrictions that "
                "cannot be evaluated against the selected tool's provider "
                "declarations -- declare provider capability tags or "
                "relax the policy"
            )
        if (denied_keys or allow_keys) and operation_keys is None:
            label = policy.name or "unnamed"
            reasons.append(
                f"target policy '{label}' sets explicit operation restrictions that "
                "cannot be evaluated against the selected tool's provider "
                "declarations -- declare provider operations with a capability "
                "namespace or "
                "relax the policy"
            )
    return reasons


def _command_text_candidates(
    arguments: Mapping[str, Any], command_fields: Sequence[str]
) -> list[str]:
    """Collect command text from declared fields only."""
    candidates: list[str] = []
    if not isinstance(arguments, Mapping):
        return candidates
    for field_name in command_fields:
        if not isinstance(field_name, str):
            continue
        if field_name not in arguments:
            continue
        value = arguments[field_name]
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, (list, tuple)):
            candidates.extend(item for item in value if isinstance(item, str))
    return candidates


def match_command_rules(
    arguments: Mapping[str, Any],
    command_fields: Sequence[str] | None,
    rules: Sequence[CommandApprovalRule],
    risk: RiskLevel | None,
) -> list[str]:
    firing: list[str] = []
    candidates = _command_text_candidates(arguments, command_fields or ())
    for index, rule in enumerate(rules):
        patterns = [
            pattern
            for pattern in (rule.patterns or [])
            if isinstance(pattern, str) and pattern.strip()
        ]
        if not patterns:
            continue
        if (
            rule.minimum_risk is not None
            and risk is not None
            and not risk_meets_threshold(risk, rule.minimum_risk)
        ):
            continue
        label = rule.name.strip() or f"rule-{index}"
        if any(matches_prohibited_patterns(candidate, patterns) for candidate in candidates):
            firing.append(f"{COMMAND_MATCH_PREFIX}{label}")
    return firing


def evaluate_policy(
    action: ActionProposal,
    approval_policy: ApprovalPolicy,
    *,
    risk: RiskLevel | None = None,
    risk_threshold: RiskLevel | None = None,
    selected: ToolDescriptor | None = None,
    has_selected_tool: bool = False,
) -> tuple[PolicyVerdict, list[str], list[str]]:
    require = approval_policy.require_approval_for
    action_key = f"{action.capability}.{action.operation}"

    if action.capability in require:
        return (
            PolicyVerdict.REQUIRE_APPROVAL,
            [f"capability '{action.capability}' requires approval"],
            [action.capability],
        )
    if action_key in require:
        return (
            PolicyVerdict.REQUIRE_APPROVAL,
            [f"operation '{action_key}' requires approval"],
            [action_key],
        )

    if require and has_selected_tool:
        namespaces = declared_namespaces(selected)
        operation_keys = declared_operation_keys(selected)
        if namespaces is None and operation_keys is None:
            return (
                PolicyVerdict.REQUIRE_APPROVAL,
                [
                    "policy semantics unavailable: the selected tool declares "
                    "no operations/capability tags to evaluate the explicit "
                    "approval policy against -- declare provider operations or "
                    "relax require_approval_for"
                ],
                [POLICY_SEMANTICS_UNAVAILABLE],
            )
        for entry in require:
            if "." in entry:
                matched = operation_keys is not None and entry in operation_keys
            else:
                matched = namespaces is not None and entry in namespaces
            if matched:
                return (
                    PolicyVerdict.REQUIRE_APPROVAL,
                    [f"operation '{entry}' requires approval (provider-declared)"],
                    [entry],
                )
        need_namespaces = any("." not in entry for entry in require)
        need_operation_keys = any("." in entry for entry in require)
        if (need_namespaces and namespaces is None) or (
            need_operation_keys and operation_keys is None
        ):
            return (
                PolicyVerdict.REQUIRE_APPROVAL,
                [
                    "policy semantics unavailable: the selected tool declares "
                    "no operations/capability tags to evaluate the explicit "
                    "approval policy against -- declare provider operations or "
                    "relax require_approval_for"
                ],
                [POLICY_SEMANTICS_UNAVAILABLE],
            )

    explicit_rules = list(getattr(approval_policy, "command_approval_rules", None) or [])
    if explicit_rules:
        command_rules = explicit_rules
    elif has_selected_tool:
        command_rules = default_command_approval_rules()
    else:
        command_rules = []
    if command_rules:
        if not has_selected_tool or selected is None:
            return (
                PolicyVerdict.REQUIRE_APPROVAL,
                [
                    "policy semantics unavailable: command approval rules need "
                    "a selected tool to evaluate -- declare the calling tool or "
                    "remove command_approval_rules"
                ],
                [POLICY_SEMANTICS_UNAVAILABLE],
            )
        command_fields = getattr(selected, "command_fields", None)
        if command_fields is None:
            return (
                PolicyVerdict.REQUIRE_APPROVAL,
                [
                    "policy semantics unavailable: the selected tool declares "
                    "no command fields to evaluate command approval rules -- "
                    "declare provider command fields or remove command_approval_rules"
                ],
                [POLICY_SEMANTICS_UNAVAILABLE],
            )
        firing = match_command_rules(action.arguments, command_fields, command_rules, risk)
        if firing:
            return (
                PolicyVerdict.REQUIRE_APPROVAL,
                [f"dangerous command rule matched ({', '.join(firing)}) -- requiring approval"],
                firing,
            )

    # Risk-tiered approval: the tool's registry risk_level meets the
    # approval threshold, so a human must sign off even without an
    # explicit require_approval_for entry. An explicit ``risk_threshold``
    # wins; otherwise the engagement's configured value is used, falling
    # back to the Engine default.
    effective_threshold = (
        risk_threshold if risk_threshold is not None else resolve_risk_threshold(approval_policy)
    )
    if risk is not None and risk_meets_threshold(risk, effective_threshold):
        return (
            PolicyVerdict.REQUIRE_APPROVAL,
            [
                f"tool risk_level '{risk.value}' requires approval "
                f"(>= '{effective_threshold.value}')"
            ],
            [f"risk:{risk.value}"],
        )

    return PolicyVerdict.ALLOW, ["no matching approval rule"], []
