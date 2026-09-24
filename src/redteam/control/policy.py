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
registry's value rather than hardcoding one. The ``risk_threshold`` is a
placeholder default for now (see ``redteam.control.risk``); making it a
per-engagement ``ApprovalPolicy`` field is a deferred design decision.
"""

from __future__ import annotations

from redteam.control.risk import risk_meets_threshold
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.enums import PolicyVerdict, RiskLevel


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
    # explicit require_approval_for entry.
    if risk is not None and risk_threshold is not None and risk_meets_threshold(
        risk, risk_threshold
    ):
        return (
            PolicyVerdict.REQUIRE_APPROVAL,
            [f"tool risk_level '{risk.value}' requires approval (>= '{risk_threshold.value}')"],
            [f"risk:{risk.value}"],
        )

    return PolicyVerdict.ALLOW, ["no matching approval rule"], []
