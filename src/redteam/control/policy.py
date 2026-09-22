"""Policy Engine — decides ALLOW vs. REQUIRE_APPROVAL for in-scope,
ROE-permitted actions.

Division of responsibility:
  - **Denials** are ROE's job (``redteam.control.roe`` —
    ``denied_capabilities`` / ``denied_operations``). The policy engine
    does not deny; it gates actions behind human approval.
  - This stage only decides whether an otherwise-permitted action needs
    approval, using ``ApprovalPolicy.require_approval_for``.

Matching (consistent with ROE and ``docs/contracts/actions.md``):
an ``require_approval_for`` entry matches either
  - a ``capability`` namespace (e.g. ``"mobile"`` gates all mobile
    actions), or
  - a canonical action key ``<capability>.<operation>`` (e.g.
    ``"recon.port_scan"`` gates just that action).

TODO(control-team, track-a): risk-level-based approval needs the tool's
``risk_level`` — but that lives on ``ToolDescriptor``, only known after
tool selection (post-ALLOW), not on the ``ActionProposal`` this stage
sees. Blocked on the same "signal absent from ActionProposal" gap as the
remaining ROE fields.
"""

from __future__ import annotations

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.enums import PolicyVerdict


def evaluate_policy(
    action: ActionProposal, approval_policy: ApprovalPolicy
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
    return PolicyVerdict.ALLOW, ["no matching approval rule"], []
