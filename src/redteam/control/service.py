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

Accounting boundaries: ``BudgetTracker`` / ``RateLimiter`` are per-process
and in-memory, so a rebuilt plane (durable resume) starts empty. ``rehydrate``
is the explicit, bounded, monotonic restore of those counters from the
counters a caller read off the durable checkpoint; ``record_tool_calls`` is
the seam a bounded retry must debit so retries cannot exceed the
authoritative budget. A rate slot and a budget action are each charged
exactly once per admitted action — a ``REQUIRE_APPROVAL`` decision charges
nothing, and ``approval_outcome`` charges once if/when approved.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.budget import BudgetTracker
from redteam.control.policy import (
    evaluate_policy,
    may_auto_approve,
    resolve_risk_threshold,
)
from redteam.control.rate_limit import RateLimiter
from redteam.control.risk import RiskResolver
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

#: Upper bound on the number of counters ``rehydrate`` accepts. Counters are
#: clamped to the engagement's own caps anyway; this is a defensive ceiling
#: against an absurd value (e.g. a corrupt checkpoint) ever being materialised
#: as a deque/list. It is far above any realistic engagement budget.
_MAX_REHYDRATABLE_COUNT = 1_000_000


@dataclass(frozen=True)
class RehydratedAccounting:
    """The authoritative counters a fresh process restored via ``rehydrate``.

    Returned so a durable-resume caller can log/verify what was restored
    (and what remains) without reaching into Control internals. Contains
    only bounded integer counters and ids -- no proposal contents, tool
    output, or secrets.
    """

    engagement_id: EngagementId
    actions_consumed: int
    tool_calls_consumed: int
    rate_actions_in_window: int
    budget_remaining: int
    rate_remaining: int


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
        # restart on its own; a durable resume re-establishes it from the
        # checkpoint with register_pending_proposal() (never by guessing a
        # proposal back from Audit). See approval_outcome()'s "stale
        # approval" handling. Popped by approval_outcome() once consumed.
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
        approval_policy = engagement.approval_policy
        verdict, policy_reasons, matched_rules = evaluate_policy(
            proposal,
            approval_policy,
            risk=risk,
            # Engagement-configured threshold, else the Engine default.
            risk_threshold=resolve_risk_threshold(approval_policy),
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
        # Non-consuming admission check. The rate slot is charged exactly
        # once, at the point the action is actually admitted (below for
        # ALLOW, or in ``approval_outcome`` once a human approves). Charging
        # here as well would charge a REQUIRE_APPROVAL action twice.
        if not rate_limiter.would_allow():
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
        # Bounded auto-approval: legacy ``auto_approve`` maps to LOW;
        # ``auto_approve_up_to`` configures an explicit ceiling. Unknown
        # risk never qualifies.
        if requires_approval and not may_auto_approve(approval_policy, risk):
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
            # No rate slot and no budget action charged: nothing executed.
            # ``approval_outcome`` charges both once, if/when approved.
            return decision

        # Admitted: charge exactly one rate slot and one budget action.
        if not rate_limiter.allow():  # defensive: would_allow already checked
            return await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                ["rate limit exceeded"],
                ["rate_limit"],
                rate_limit_remaining=rate_limiter.remaining,
                severity=Severity.BLOCK_ACTION,
            )
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
        self, approval_id: ApprovalId | str, status: ApprovalStatus, resolved_by: str
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
        normalized_id = ApprovalId(str(approval_id))
        approval = await self._approvals.resolve(normalized_id, status, resolved_by)
        proposal = self._pending_proposals.get(normalized_id)
        if proposal is not None:
            await self._audit.record_approval_resolution(proposal, approval)
        return approval

    async def register_pending_proposal(
        self, approval_id: ApprovalId | str, proposal: ActionProposal
    ) -> Approval:
        """Re-link the checkpointed proposal a pending Approval belongs to.

        Durable-resume counterpart to the in-memory bookkeeping ``evaluate``
        does when it mints an Approval: a fresh process has the Approval
        (durable ``ApprovalStore``) and the original ``ActionProposal``
        (durable checkpoint) but no process-local link between them. This
        restores exactly that link so ``approval_outcome`` re-checks rate
        limit/budget against the *original* proposal instead of failing
        closed as a stale approval.

        The proposal is never reconstructed from the (redacted, lossy) Audit
        snapshot -- the caller must supply the checkpointed proposal, and it
        must match the Approval's own ``action_id`` exactly. A mismatch
        raises rather than ever linking a different action. Idempotent:
        repeating the same registration is a no-op; a conflicting proposal
        already linked to this Approval raises.
        """
        normalized_id = ApprovalId(str(approval_id))
        approval = await self._approvals.get(normalized_id)
        if approval is None:
            raise KeyError(f"no such approval: {approval_id}")
        if approval.action_id != proposal.action_id:
            raise ValueError(
                f"approval {normalized_id} is for action {approval.action_id}; "
                f"refusing to link proposal {proposal.action_id}"
            )
        if proposal.engagement_id not in self._engagements:
            raise KeyError(
                f"proposal {proposal.action_id} references unregistered "
                f"engagement {proposal.engagement_id}"
            )
        existing = self._pending_proposals.get(normalized_id)
        if existing is not None:
            if existing != proposal:
                raise ValueError(
                    f"approval {normalized_id} is already linked to a different proposal"
                )
            return approval
        self._pending_proposals[normalized_id] = proposal
        return approval

    async def approval_outcome(self, approval_id: ApprovalId | str) -> PolicyDecision | None:
        normalized_id = ApprovalId(str(approval_id))
        cached = self._resolved_outcomes.get(normalized_id)
        if cached is not None:
            return cached

        approval = await self._approvals.get(normalized_id)
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
                approval_id=normalized_id,
                evaluated_at=datetime.now(timezone.utc),
            )
            self._resolved_outcomes[normalized_id] = decision
            return decision

        proposal = self._pending_proposals.pop(normalized_id, None)
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
                approval_id=normalized_id,
                evaluated_at=datetime.now(timezone.utc),
            )
            self._resolved_outcomes[normalized_id] = decision
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
        # Charge the rate slot exactly once, on actual admission -- the
        # original REQUIRE_APPROVAL evaluation only *checked* the window
        # (would_allow) and charged nothing, so charging here is the single
        # charge for this action, not a second one.
        if not rate_limiter.would_allow():
            decision = await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                [f"approval {approval_id} was approved, but rate limit is exceeded now"],
                ["approval", "rate_limit"],
                rate_limit_remaining=rate_limiter.remaining,
                severity=Severity.BLOCK_ACTION,
                approval_id=normalized_id,
            )
            self._resolved_outcomes[normalized_id] = decision
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
                approval_id=normalized_id,
            )
            self._resolved_outcomes[normalized_id] = decision
            return decision

        if not rate_limiter.allow():  # defensive: would_allow already checked
            decision = await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                [f"approval {approval_id} was approved, but rate limit is exceeded now"],
                ["approval", "rate_limit"],
                rate_limit_remaining=rate_limiter.remaining,
                severity=Severity.BLOCK_ACTION,
                approval_id=normalized_id,
            )
            self._resolved_outcomes[normalized_id] = decision
            return decision
        budget.consume()
        decision = await self._finalize(
            proposal,
            PolicyVerdict.ALLOW,
            [f"approval {approval_id} approved by {approval.resolved_by}"],
            ["approval"],
            rate_limit_remaining=rate_limiter.remaining,
            budget_remaining=budget.remaining,
            approval_id=normalized_id,
        )
        self._resolved_outcomes[normalized_id] = decision
        return decision

    async def rehydrate(
        self,
        engagement_id: EngagementId | str,
        *,
        actions_consumed: int,
        tool_calls_consumed: int | None = None,
        rate_actions_in_window: int | None = None,
        elapsed_duration_seconds: float | None = None,
    ) -> RehydratedAccounting:
        """Restore a fresh process's accounting for an already-running run.

        A new ``DefaultControlPlane`` (e.g. one rebuilt by ``redteam resume``)
        starts with empty rate/budget counters, so without this it would hand
        a resumed run a *fresh* full budget and window. This explicit method
        re-seeds those counters from the counters the caller read off the
        durable checkpoint/``RuntimeState``.

        Contract (all enforced here, so a bad caller cannot widen budget):

        - ``engagement_id`` must be one this plane was built for; the
          original run's id (from the checkpoint) is what ``redteam resume``
          already passes to ``build_application``. Unknown -> ``KeyError``.
        - Only bounded integer *counters* are accepted -- never proposal
          contents, tool output, audit snapshots, or secrets. Values are
          clamped to the engagement's own limits
          (``ExecutionBudget.max_actions`` / ``max_tool_calls`` and
          ``RateLimitPolicy.max_actions_per_minute``) and additionally to a
          defensive ceiling.
        - The restore is monotonic: counters are only ever raised, never
          lowered, so calling this again (or with smaller numbers) can never
          return already-consumed capacity.
        - ``actions_consumed`` is the run's executed-action count;
          ``tool_calls_consumed`` defaults to it and, when supplied, lets a
          run that used bounded retries restore the extra tool calls the
          authoritative budget must also account for. ``max_tool_calls`` and
          the action cap therefore still bind after a restart.
        - ``rate_actions_in_window`` defaults to ``actions_consumed``
          (conservative: assume the whole run fell in the trailing window).
          The sliding window itself is not durable, so restored events are
          placed at "now" and age out one window later.
        - ``elapsed_duration_seconds`` is the wall-clock time the original
          run already spent, persisted by the resume caller (e.g. derived
          from the checkpointed run's start instant). It is validated
          (finite, non-negative) and clamped to the engagement's
          ``max_duration_seconds`` before restore. When a duration cap is
          configured and no elapsed value is supplied, the restore fails
          closed -- the duration is treated as already spent instead of
          restarting the full window on resume. It has no effect on an
          engagement with no duration cap.

        Scope/ROE/Policy are not touched: they are deterministic against the
        immutable engagement config and need no restoration.
        """
        normalized_id = EngagementId(str(engagement_id))
        engagement = self._engagements.get(normalized_id)
        if engagement is None:
            raise KeyError(f"unregistered engagement: {engagement_id}")

        def _bounded(value: int, *, name: str, ceiling: int) -> int:
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")
            if value > _MAX_REHYDRATABLE_COUNT:
                raise ValueError(
                    f"{name}={value} exceeds the maximum rehydratable count "
                    f"({_MAX_REHYDRATABLE_COUNT})"
                )
            return min(value, ceiling)

        actions = _bounded(
            actions_consumed,
            name="actions_consumed",
            ceiling=engagement.execution_budget.max_actions,
        )
        tool_calls = None
        if tool_calls_consumed is not None:
            tool_calls = _bounded(
                tool_calls_consumed,
                name="tool_calls_consumed",
                ceiling=(
                    engagement.execution_budget.max_tool_calls
                    if engagement.execution_budget.max_tool_calls is not None
                    else engagement.execution_budget.max_actions
                ),
            )
        window_actions = (
            actions
            if rate_actions_in_window is None
            else _bounded(
                rate_actions_in_window,
                name="rate_actions_in_window",
                ceiling=engagement.rate_limit_policy.max_actions_per_minute,
            )
        )

        elapsed = elapsed_duration_seconds
        if elapsed is not None:
            if not math.isfinite(elapsed):
                raise ValueError(
                    f"elapsed_duration_seconds must be finite, got {elapsed!r}"
                )
            if elapsed < 0:
                raise ValueError(
                    f"elapsed_duration_seconds must be non-negative, got {elapsed}"
                )
            max_duration = engagement.execution_budget.max_duration_seconds
            if max_duration is not None:
                elapsed = min(elapsed, float(max_duration))

        budget = self._budgets.setdefault(
            normalized_id, BudgetTracker(engagement.execution_budget)
        )
        budget.rehydrate(
            actions_consumed=actions,
            tool_calls_consumed=tool_calls,
            elapsed_duration_seconds=elapsed,
        )
        rate_limiter = self._rate_limiters.setdefault(
            normalized_id, RateLimiter(engagement.rate_limit_policy)
        )
        rate_limiter.rehydrate(window_actions)

        logger.info(
            "rehydrated control accounting for engagement %s: actions=%d tool_calls=%d",
            normalized_id,
            budget.consumed,
            budget.tool_calls_consumed,
            extra={"engagement_id": str(normalized_id), "run_restart": True},
        )
        return RehydratedAccounting(
            engagement_id=normalized_id,
            actions_consumed=budget.consumed,
            tool_calls_consumed=budget.tool_calls_consumed,
            rate_actions_in_window=rate_limiter.in_window,
            budget_remaining=budget.remaining,
            rate_remaining=rate_limiter.remaining,
        )

    async def record_tool_calls(
        self, engagement_id: EngagementId | str, count: int = 1
    ) -> int:
        """Charge ``count`` extra tool calls against the authoritative budget.

        Bounded-retry counterpart to ``BudgetTracker.consume_tool_calls``:
        one admitted action already charged one action + one tool call at
        ``evaluate`` time, and every retry attempt re-invokes the tool, so
        the retry must be debited here too or it would exceed the
        engagement's ``max_tool_calls``/``max_actions`` at the Control Plane
        (the only authoritative budget; ``RuntimeState.remaining_budget`` is
        a local mirror).

        Returns the remaining budget after the charge. Raises ``KeyError``
        for an unknown engagement and ``ValueError`` for a negative count.
        The tracker must already exist (``evaluate`` creates it on
        admission, and ``rehydrate`` restores it on resume); if it does not,
        this creates a fresh one so the charge is still counted, but callers
        should treat a missing tracker as a resume bug and rehydrate first.
        """
        normalized_id = EngagementId(str(engagement_id))
        engagement = self._engagements.get(normalized_id)
        if engagement is None:
            raise KeyError(f"unregistered engagement: {engagement_id}")
        if count < 0:
            raise ValueError(f"tool-call count must be non-negative, got {count}")
        budget = self._budgets.setdefault(
            normalized_id, BudgetTracker(engagement.execution_budget)
        )
        budget.consume_tool_calls(count)
        return budget.remaining

    async def has_tool_call_capacity(self, engagement_id: EngagementId | str) -> bool:
        """Whether the authoritative budget still permits one more tool call.

        Bounded-retry callers should check this *before* re-invoking a tool
        so a retry is never attempted once the engagement's
        ``max_tool_calls`` / ``max_actions`` (whichever binds first) is
        exhausted. A missing tracker (no action admitted yet, no rehydrate
        needed) reports capacity, matching a fresh tracker's initial state;
        an unknown engagement raises ``KeyError``.
        """
        normalized_id = EngagementId(str(engagement_id))
        engagement = self._engagements.get(normalized_id)
        if engagement is None:
            raise KeyError(f"unregistered engagement: {engagement_id}")
        budget = self._budgets.get(normalized_id)
        if budget is None:
            return True
        return budget.tool_calls_remaining > 0

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
