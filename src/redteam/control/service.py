"""``DefaultControlPlane`` — implements ``redteam.core.ports.control_plane.ControlPlane``.

Pipeline (instruction.md section 9): Scope -> ROE -> Policy -> Rate
Limit -> Budget -> Approval -> ALLOW / DENY / REQUIRE_APPROVAL.

This is a code-based gate: every branch below returns a concrete
``PolicyDecision`` before a ``ToolInvocation`` can be built, so ALLOW
cannot happen by omission. ``context.engagement_id`` selects which
``Engagement`` config (scope/budget/ROE/policy) applies.

TODO(control-team): real ROE stage (currently pass-through — no ROE
rule evaluation implemented yet), per-engagement audit sinks.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.budget import BudgetTracker
from redteam.control.policy import evaluate_policy
from redteam.control.rate_limit import RateLimiter
from redteam.control.scope import check_scope
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import EngagementId


class DefaultControlPlane:
    """The reference ``ControlPlane`` wired in ``app.bootstrap`` by default.

    "Default" means "what you get out of the box," not "production-
    complete" — Scope/Policy/RateLimit/Budget are real, enforced logic
    (not a stub that always returns ALLOW), but ROE is a pass-through
    and the policy engine is minimal rule matching; see the
    module-level TODO(control-team) and each submodule
    (``redteam.control.scope``/``policy``/``rate_limit``/``budget``)
    for exactly what's still open.
    """

    def __init__(
        self,
        engagements: dict[EngagementId, Engagement],
        audit_log: AuditLog | None = None,
    ) -> None:
        self._engagements = engagements
        self._audit = audit_log or AuditLog()
        self._rate_limiters: dict[EngagementId, RateLimiter] = {}
        self._budgets: dict[EngagementId, BudgetTracker] = {}
        self._approvals = ApprovalService()

    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision:
        engagement_id = context.engagement_id
        engagement = self._engagements[engagement_id]

        scope_ok, scope_reasons = check_scope(proposal, engagement.scope)
        if not scope_ok:
            return self._finalize(proposal, PolicyVerdict.DENY, scope_reasons, ["scope"])

        # ROE stage: pass-through for now — see module docstring TODO.

        verdict, policy_reasons, matched_rules = evaluate_policy(
            proposal, engagement.approval_policy
        )
        if verdict == PolicyVerdict.DENY:
            return self._finalize(proposal, verdict, policy_reasons, matched_rules)

        rate_limiter = self._rate_limiters.setdefault(
            engagement_id, RateLimiter(engagement.rate_limit_policy)
        )
        if not rate_limiter.allow():
            return self._finalize(
                proposal,
                PolicyVerdict.DENY,
                ["rate limit exceeded"],
                ["rate_limit"],
                rate_limit_remaining=rate_limiter.remaining,
            )

        budget = self._budgets.setdefault(
            engagement_id, BudgetTracker(engagement.execution_budget)
        )
        if not budget.has_capacity():
            return self._finalize(
                proposal,
                PolicyVerdict.DENY,
                ["execution budget exhausted"],
                ["budget"],
                budget_remaining=budget.remaining,
            )

        requires_approval = verdict == PolicyVerdict.REQUIRE_APPROVAL
        if requires_approval and not engagement.approval_policy.auto_approve:
            approval = await self._approvals.request(proposal.action_id)
            decision = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reasons=policy_reasons,
                matched_rules=matched_rules,
                approval_id=approval.approval_id,
                rate_limit_remaining=rate_limiter.remaining,
                budget_remaining=budget.remaining,
                evaluated_at=datetime.now(timezone.utc),
            )
            self._audit.record(proposal, decision)
            return decision

        budget.consume()
        return self._finalize(
            proposal,
            PolicyVerdict.ALLOW,
            policy_reasons or ["allowed"],
            matched_rules,
            rate_limit_remaining=rate_limiter.remaining,
            budget_remaining=budget.remaining,
        )

    def _finalize(
        self,
        proposal: ActionProposal,
        verdict: PolicyVerdict,
        reasons: list[str],
        matched_rules: list[str],
        *,
        rate_limit_remaining: int | None = None,
        budget_remaining: int | None = None,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            action_id=proposal.action_id,
            verdict=verdict,
            reasons=reasons,
            matched_rules=matched_rules,
            rate_limit_remaining=rate_limit_remaining,
            budget_remaining=budget_remaining,
            evaluated_at=datetime.now(timezone.utc),
        )
        self._audit.record(proposal, decision)
        return decision
