"""``DefaultControlPlane`` — implements ``redteam.core.ports.control_plane.ControlPlane``.

Pipeline (instruction.md section 9): Scope -> ROE -> Policy -> Rate
Limit -> Budget -> Approval -> ALLOW / DENY / REQUIRE_APPROVAL.

This is a code-based gate: every branch below returns a concrete
``PolicyDecision`` before a ``ToolInvocation`` can be built, so ALLOW
cannot happen by omission. ``context.engagement_id`` selects which
``Engagement`` config (scope/budget/ROE/policy) applies.

TODO(control-team): ROE stage enforces capability/operation allow/deny
lists and time windows (``redteam.control.roe``); still blocked there —
availability impact, prohibited techniques, max_parallel (need signals
absent from ActionProposal, track-a). Per-engagement durable audit
sinks (track-b).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.budget import BudgetTracker
from redteam.control.policy import evaluate_policy
from redteam.control.rate_limit import RateLimiter
from redteam.control.roe import check_roe
from redteam.control.scope import check_scope
from redteam.control.severity import Severity, to_tag
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.policy import Approval, ControlContext, PolicyDecision
from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ApprovalId, EngagementId


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
        approvals: ApprovalService | None = None,
    ) -> None:
        self._engagements = engagements
        self._audit = audit_log or AuditLog()
        self._rate_limiters: dict[EngagementId, RateLimiter] = {}
        self._budgets: dict[EngagementId, BudgetTracker] = {}
        self._approvals = approvals or ApprovalService()
        # Remembers which ActionProposal a pending Approval belongs to, so
        # resolve_approval() can record an APPROVAL_RESOLVED audit entry
        # correlated back to run_id/engagement_id/action_id. Popped once
        # resolved -- not a durable store itself, just enough bookkeeping
        # to bridge evaluate() and resolve_approval().
        self._pending_proposals: dict[ApprovalId, ActionProposal] = {}

    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision:
        engagement_id = context.engagement_id
        engagement = self._engagements[engagement_id]

        scope_ok, scope_reasons, scope_severity = check_scope(proposal, engagement.scope)
        if not scope_ok:
            return await self._finalize(
                proposal, PolicyVerdict.DENY, scope_reasons, ["scope"], severity=scope_severity
            )

        roe_ok, roe_reasons, roe_severity = check_roe(proposal, engagement)
        if not roe_ok:
            return await self._finalize(
                proposal, PolicyVerdict.DENY, roe_reasons, ["roe"], severity=roe_severity
            )

        verdict, policy_reasons, matched_rules = evaluate_policy(
            proposal, engagement.approval_policy
        )
        if verdict == PolicyVerdict.DENY:
            # A policy deny rejects this class of action — switch goals.
            return await self._finalize(
                proposal, verdict, policy_reasons, matched_rules, severity=Severity.REPLAN_GOAL
            )

        rate_limiter = self._rate_limiters.setdefault(
            engagement_id, RateLimiter(engagement.rate_limit_policy)
        )
        if not rate_limiter.allow():
            # Transient — retry a (later) action; don't end the run.
            return await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                ["rate limit exceeded"],
                ["rate_limit"],
                rate_limit_remaining=rate_limiter.remaining,
                severity=Severity.BLOCK_ACTION,
            )

        budget = self._budgets.setdefault(
            engagement_id, BudgetTracker(engagement.execution_budget)
        )
        if not budget.has_capacity():
            # No capacity left for any further action — end the run.
            return await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                ["execution budget exhausted"],
                ["budget"],
                budget_remaining=budget.remaining,
                severity=Severity.ABORT_RUN,
            )

        requires_approval = verdict == PolicyVerdict.REQUIRE_APPROVAL
        if requires_approval and not engagement.approval_policy.auto_approve:
            approval = await self._approvals.request(proposal.action_id)
            self._pending_proposals[approval.approval_id] = proposal
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
            await self._audit.record(proposal, decision)
            return decision

        budget.consume()
        return await self._finalize(
            proposal,
            PolicyVerdict.ALLOW,
            policy_reasons or ["allowed"],
            matched_rules,
            rate_limit_remaining=rate_limiter.remaining,
            budget_remaining=budget.remaining,
        )

    async def resolve_approval(
        self, approval_id: ApprovalId, status: ApprovalStatus, resolved_by: str
    ) -> Approval:
        """Record a human's decision on a pending Approval.

        Not part of the ``ControlPlane`` Protocol (that only requires
        ``evaluate``) -- a ``DefaultControlPlane``-specific extension,
        called by whatever notification channel (CLI/Slack/webhook)
        surfaces the pending approval to a human. Also records an
        APPROVAL_RESOLVED audit entry, closing the gap where a
        REQUIRE_APPROVAL verdict was audited but the eventual human
        decision was not.
        """
        approval = await self._approvals.resolve(approval_id, status, resolved_by)
        proposal = self._pending_proposals.pop(approval_id, None)
        if proposal is not None:
            await self._audit.record_approval_resolution(proposal, approval)
        return approval

    async def _finalize(
        self,
        proposal: ActionProposal,
        verdict: PolicyVerdict,
        reasons: list[str],
        matched_rules: list[str],
        *,
        rate_limit_remaining: int | None = None,
        budget_remaining: int | None = None,
        severity: Severity | None = None,
    ) -> PolicyDecision:
        # Carry the denial severity as a matched_rules tag (PolicyDecision
        # has no severity field yet — see redteam.control.severity).
        if severity is not None:
            matched_rules = [*matched_rules, to_tag(severity)]
        decision = PolicyDecision(
            action_id=proposal.action_id,
            verdict=verdict,
            reasons=reasons,
            matched_rules=matched_rules,
            rate_limit_remaining=rate_limit_remaining,
            budget_remaining=budget_remaining,
            evaluated_at=datetime.now(timezone.utc),
        )
        await self._audit.record(proposal, decision)
        return decision
