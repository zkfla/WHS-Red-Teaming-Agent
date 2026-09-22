"""Policy Engine — decides ALLOW vs. REQUIRE_APPROVAL for in-scope actions.

Deliberately simple rule matching for this stage (instruction.md
section 6: "세부 Policy 구현보다 Schema와 Interface 정의가 중요"). A
real rule engine (OPA, custom DSL, ...) can replace the body of
``evaluate_policy`` later without changing its signature.

TODO(control-team): richer rule model (not just "capability:operation"
strings), per-risk-level rules, deny rules (not just approval gating).
"""

from __future__ import annotations

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.enums import PolicyVerdict


def evaluate_policy(
    action: ActionProposal, approval_policy: ApprovalPolicy
) -> tuple[PolicyVerdict, list[str], list[str]]:
    rule_key = f"{action.capability}:{action.operation}"
    if rule_key in approval_policy.require_approval_for:
        return PolicyVerdict.REQUIRE_APPROVAL, [f"rule '{rule_key}' requires approval"], [rule_key]
    return PolicyVerdict.ALLOW, ["no matching deny/approval rule"], []
