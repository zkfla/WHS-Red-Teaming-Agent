"""Policy Engine — decides ALLOW vs. REQUIRE_APPROVAL for in-scope,
ROE-permitted actions.

Division of responsibility:
  - **Denials** are ROE's job (``redteam.control.roe`` —
    ``denied_capabilities`` / ``denied_operations``). The policy engine
    does not deny; it gates actions behind human approval.
  - This stage decides whether an otherwise-permitted action needs
    approval, from two sources:
      1. ``ApprovalPolicy.require_approval_for`` (operator-listed
         capabilities / canonical action keys), and
      2. **risk-tiered approval** — when the action's risk (resolved from
         the Capability Tool Registry, see ``redteam.control.risk``) meets
         a threshold, approval is required regardless of the list.

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

from redteam.control.risk import (
    DEFAULT_APPROVAL_RISK_THRESHOLD,
    risk_at_or_below,
    risk_meets_threshold,
)
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.enums import PolicyVerdict, RiskLevel


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


def evaluate_policy(
    action: ActionProposal,
    approval_policy: ApprovalPolicy,
    *,
    risk: RiskLevel | None = None,
    risk_threshold: RiskLevel | None = None,
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
