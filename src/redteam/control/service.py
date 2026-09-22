"""``DefaultControlPlane`` — implements ``redteam.core.ports.control_plane.ControlPlane``.

Pipeline (instruction.md section 9): Scope -> ROE -> Policy -> Rate
Limit -> Budget -> Approval -> ALLOW / DENY / REQUIRE_APPROVAL.

This is a code-based gate: every branch below returns a concrete
``PolicyDecision`` before a ``ToolInvocation`` can be built, so ALLOW
cannot happen by omission. ``context.engagement_id`` selects which
``Engagement`` config (scope/budget/ROE/policy) applies.

ROE (``redteam.control.roe``) enforces capability/operation allow/deny
lists, time windows, availability impact, and max_parallel_actions
(the last via ``context.metadata["in_flight_actions"]`` — Orchestration
supplies it; Control never imports the Work Ledger). ``prohibited_techniques``
has no enforcement path yet and fails the run closed rather than being
silently ignored — see ``roe.UnsupportedRoePolicyError``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.budget import BudgetTracker
from redteam.control.policy import evaluate_policy
from redteam.control.rate_limit import RateLimiter
from redteam.control.risk import DEFAULT_APPROVAL_RISK_THRESHOLD, RiskResolver
from redteam.control.roe import UnsupportedRoePolicyError, check_roe
from redteam.control.scope import check_scope
from redteam.control.severity import Severity, to_tag
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.policy import Approval, ControlContext, PolicyDecision
from redteam.core.enums import ApprovalStatus, PolicyVerdict, RiskLevel
from redteam.core.ids import ApprovalId, EngagementId
from redteam.evidence.service import EvidenceService

logger = logging.getLogger(__name__)


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
        evidence_service: EvidenceService | None = None,
        risk_resolver: RiskResolver | None = None,
    ) -> None:
        self._engagements = engagements
        self._audit = audit_log or AuditLog()
        self._rate_limiters: dict[EngagementId, RateLimiter] = {}
        self._budgets: dict[EngagementId, BudgetTracker] = {}
        self._approvals = approvals or ApprovalService()
        # Optional risk-tiered approval: resolves an action's risk from the
        # Capability Tool Registry (source of truth) so higher-risk actions
        # require human approval. None = no risk-based gating (unchanged).
        self._risk_resolver = risk_resolver
        # Optional (Track B's EvidenceService): when present, DENY /
        # REQUIRE_APPROVAL decisions are also recorded as Evidence so the
        # final report includes blocked attempts, not just executed ones.
        # See docs/control/blocked-action-evidence.md.
        self._evidence_service = evidence_service
        # Remembers which ActionProposal a pending Approval belongs to, so
        # resolve_approval() can record an APPROVAL_RESOLVED audit entry,
        # and approval_outcome() can re-evaluate rate-limit/budget against
        # the original proposal once a human resolves it. In-memory only
        # -- not a durable store -- so this does not survive a process
        # restart; see approval_outcome()'s "stale approval" handling.
        # Popped by approval_outcome() once consumed.
        self._pending_proposals: dict[ApprovalId, ActionProposal] = {}
        # Cached so approval_outcome() is idempotent: a second call after
        # the first resolved one returns the same PolicyDecision instead
        # of re-consuming rate-limit/budget capacity.
        self._resolved_outcomes: dict[ApprovalId, PolicyDecision] = {}

    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision:
        engagement_id = context.engagement_id
        engagement = self._engagements[engagement_id]

        scope_ok, scope_reasons, scope_severity = check_scope(proposal, engagement.scope)
        if not scope_ok:
            return await self._finalize(
                proposal, PolicyVerdict.DENY, scope_reasons, ["scope"], severity=scope_severity
            )

        in_flight = context.metadata.get("in_flight_actions")
        try:
            roe_ok, roe_reasons, roe_severity = check_roe(
                proposal, engagement, in_flight_actions=in_flight
            )
        except UnsupportedRoePolicyError as exc:
            # A configured-but-unenforceable ROE field: fail the whole run
            # closed rather than let it silently pass (see roe.py). Every
            # further action against this Engagement would hit the same
            # rule, so ABORT_RUN — not just this one action.
            return await self._finalize(
                proposal, PolicyVerdict.DENY, [str(exc)], ["roe"], severity=Severity.ABORT_RUN
            )
        if not roe_ok:
            return await self._finalize(
                proposal, PolicyVerdict.DENY, roe_reasons, ["roe"], severity=roe_severity
            )

        risk, risk_unresolved = self._resolve_risk(proposal)
        verdict, policy_reasons, matched_rules = evaluate_policy(
            proposal,
            engagement.approval_policy,
            risk=risk,
            risk_threshold=DEFAULT_APPROVAL_RISK_THRESHOLD,
        )
        if risk_unresolved and verdict == PolicyVerdict.ALLOW:
            # Manual escalation, not a fail-closed DENY: risk-tiered
            # approval exists precisely to catch risky actions, so when
            # the risk lookup errors we escalate to a human rather than
            # let a possibly-risky action through on an unresolved risk.
            # A human can still approve it -- this stage only refuses to
            # decide ALLOW on its own when it doesn't know the risk.
            verdict = PolicyVerdict.REQUIRE_APPROVAL
            policy_reasons = ["tool risk_level could not be resolved — requiring approval"]
            matched_rules = ["risk:unresolved"]
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
            await self._record_blocked_action(proposal, decision)
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

        Not part of the ``ControlPlane`` Protocol -- a
        ``DefaultControlPlane``-specific extension, called by whatever
        notification channel (CLI/Slack/webhook) surfaces the pending
        approval to a human. Also records an APPROVAL_RESOLVED audit
        entry, closing the gap where a REQUIRE_APPROVAL verdict was
        audited but the eventual human decision was not.

        Only marks the human decision -- it does not re-evaluate rate
        limit/budget or build the final ``PolicyDecision``; call
        ``approval_outcome`` (the Protocol method Orchestration's resume
        path uses) for that, once. Deliberately keeps ``_pending_proposals``
        intact here (unlike before) so ``approval_outcome`` can still
        reach it afterwards.
        """
        approval = await self._approvals.resolve(approval_id, status, resolved_by)
        proposal = self._pending_proposals.get(approval_id)
        if proposal is not None:
            await self._audit.record_approval_resolution(proposal, approval)
        return approval

    async def approval_outcome(self, approval_id: ApprovalId) -> PolicyDecision | None:
        cached = self._resolved_outcomes.get(approval_id)
        if cached is not None:
            return cached

        approval = await self._approvals.get(approval_id)
        if approval is None or approval.status == ApprovalStatus.PENDING:
            return None
        action_id = approval.action_id

        if approval.status != ApprovalStatus.APPROVED:
            # REJECTED or EXPIRED: a terminal DENY, never left dangling.
            decision = PolicyDecision(
                action_id=action_id,
                verdict=PolicyVerdict.DENY,
                reasons=[f"approval {approval_id} was {approval.status.value}, not approved"],
                matched_rules=["approval", to_tag(Severity.REPLAN_GOAL)],
                approval_id=approval_id,
                evaluated_at=datetime.now(timezone.utc),
            )
            self._resolved_outcomes[approval_id] = decision
            return decision

        proposal = self._pending_proposals.pop(approval_id, None)
        if proposal is None:
            # This ControlPlane instance never held (or already consumed)
            # the original proposal -- most likely a process restart, since
            # _pending_proposals is in-memory only. Never guess the intent
            # back from a redacted Audit snapshot: fail closed, explicitly.
            decision = PolicyDecision(
                action_id=action_id,
                verdict=PolicyVerdict.DENY,
                reasons=[
                    f"stale approval {approval_id}: original ActionProposal is no longer "
                    f"held (process restart or already consumed) -- cannot safely resume"
                ],
                matched_rules=["approval", "stale", to_tag(Severity.ABORT_RUN)],
                approval_id=approval_id,
                evaluated_at=datetime.now(timezone.utc),
            )
            self._resolved_outcomes[approval_id] = decision
            return decision

        # Only rate limit and budget can change between the original
        # REQUIRE_APPROVAL and this resolution -- Scope/ROE/Policy already
        # evaluated deterministically against this same immutable proposal
        # and engagement config. Re-running them here would also risk
        # re-triggering REQUIRE_APPROVAL and minting a second Approval for
        # the same action.
        engagement = self._engagements[proposal.engagement_id]
        rate_limiter = self._rate_limiters.setdefault(
            proposal.engagement_id, RateLimiter(engagement.rate_limit_policy)
        )
        if not rate_limiter.allow():
            decision = await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                [f"approval {approval_id} was approved, but rate limit is exceeded now"],
                ["approval", "rate_limit"],
                rate_limit_remaining=rate_limiter.remaining,
                severity=Severity.BLOCK_ACTION,
                approval_id=approval_id,
            )
            self._resolved_outcomes[approval_id] = decision
            return decision

        budget = self._budgets.setdefault(
            proposal.engagement_id, BudgetTracker(engagement.execution_budget)
        )
        if not budget.has_capacity():
            decision = await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                [f"approval {approval_id} was approved, but execution budget is exhausted now"],
                ["approval", "budget"],
                budget_remaining=budget.remaining,
                severity=Severity.ABORT_RUN,
                approval_id=approval_id,
            )
            self._resolved_outcomes[approval_id] = decision
            return decision

        budget.consume()
        decision = await self._finalize(
            proposal,
            PolicyVerdict.ALLOW,
            [f"approval {approval_id} approved by {approval.resolved_by}"],
            ["approval"],
            rate_limit_remaining=rate_limiter.remaining,
            budget_remaining=budget.remaining,
            approval_id=approval_id,
        )
        self._resolved_outcomes[approval_id] = decision
        return decision

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
        approval_id: ApprovalId | None = None,
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
            approval_id=approval_id,
            rate_limit_remaining=rate_limit_remaining,
            budget_remaining=budget_remaining,
            evaluated_at=datetime.now(timezone.utc),
        )
        await self._audit.record(proposal, decision)
        await self._record_blocked_action(proposal, decision)
        return decision

    def _resolve_risk(self, proposal: ActionProposal) -> tuple[RiskLevel | None, bool]:
        """Resolve an action's risk from the Registry.

        Returns ``(risk, unresolved)``. ``unresolved`` is True only when
        the resolver *raised* — never for the normal "no eligible tool"
        case (which returns ``risk=None, unresolved=False``). A crashing
        risk lookup must not crash the gate, and must not silently let a
        possibly-risky action skip risk-tiered approval — so an
        unresolved risk escalates to ``REQUIRE_APPROVAL`` (manual
        escalation: a human can still allow it). This is deliberately
        *not* the same guarantee as a fail-closed ``DENY`` — see the ROE
        ``UnsupportedRoePolicyError`` path above for what an actual
        fail-closed refusal looks like in this file.
        """
        if self._risk_resolver is None:
            return None, False
        try:
            return self._risk_resolver.resolve(proposal.capability, proposal.operation), False
        except Exception:  # noqa: BLE001 - a broken risk source must not crash the gate
            logger.exception(
                "risk resolution failed; escalating to manual approval (risk unresolved)",
                extra={
                    "action_id": str(proposal.action_id),
                    "capability": proposal.capability,
                    "operation": proposal.operation,
                },
            )
            return None, True

    async def _record_blocked_action(
        self, proposal: ActionProposal, decision: PolicyDecision
    ) -> None:
        """Best-effort Evidence record for a non-executed (DENY /
        REQUIRE_APPROVAL) decision. Never lets an Evidence failure change
        the verdict or block the return — see
        docs/control/blocked-action-evidence.md §10.2.
        """
        if self._evidence_service is None:
            return
        if decision.verdict not in (PolicyVerdict.DENY, PolicyVerdict.REQUIRE_APPROVAL):
            return
        try:
            await self._evidence_service.record_blocked_action(proposal, decision)
        except Exception:
            logger.exception(
                "Failed to record blocked action evidence",
                extra={
                    "action_id": str(proposal.action_id),
                    "verdict": decision.verdict.value,
                },
            )
