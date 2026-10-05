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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.budget import BudgetTracker
from redteam.control.outbound import OUTBOUND_MATCHED_RULE, evaluate_with_descriptor
from redteam.control.policy import (
    COMMAND_MATCH_PREFIX,
    DEFAULT_COMMAND_APPROVAL_RULES,
    POLICY_SEMANTICS_UNAVAILABLE,
    auto_approval_ceiling,
    evaluate_policy,
    may_auto_approve,
    resolve_risk_threshold,
    unavailable_restriction_reasons,
)
from redteam.control.rate_limit import RateLimiter
from redteam.control.risk import RiskResolver
from redteam.control.roe import UnsupportedRoePolicyError, check_roe
from redteam.control.scope import check_scope
from redteam.control.severity import Severity, to_tag
from redteam.control.target_action import check_target_action_policies
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.policy import Approval, ControlContext, PolicyDecision
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ApprovalStatus, PolicyVerdict, RiskLevel
from redteam.core.ids import ApprovalId, EngagementId, ToolId
from redteam.core.tool_binding import execution_fingerprint as _binding_fingerprint
from redteam.core.tool_binding import is_direct as _is_direct_binding
from redteam.evidence.service import EvidenceService

logger = logging.getLogger(__name__)

#: Upper bound on the number of counters ``rehydrate`` accepts. Counters are
#: clamped to the engagement's own caps anyway; this is a defensive ceiling
#: against an absurd value (e.g. a corrupt checkpoint) ever being materialised
#: as a deque/list. It is far above any realistic engagement budget.
_MAX_REHYDRATABLE_COUNT = 1_000_000


def _same_action_content(first: ActionProposal, second: ActionProposal) -> bool:
    """Whether two proposals describe the same approvable action.

    Covers identity (action/run/engagement), binding (mode/tool), intent
    (capability/operation/availability) and content (target/arguments).
    Mutable mappings compare by normalized value via the shared binding
    fingerprint helper; direct/selected-tool bindings additionally require
    the same tool/target/arguments fingerprint.
    """
    if first.action_id != second.action_id:
        return False
    if getattr(first, "binding_mode", "legacy_action") != getattr(
        second, "binding_mode", "legacy_action"
    ):
        return False
    if getattr(first, "tool_id", None) != getattr(second, "tool_id", None):
        return False
    if not _binding_fingerprint_matches(first, second):
        return False
    return (
        first.capability == second.capability
        and first.operation == second.operation
        and first.target == second.target
        and first.arguments == second.arguments
        and first.availability_impact == second.availability_impact
        and first.engagement_id == second.engagement_id
        and first.run_id == second.run_id
        and first.agent_id == second.agent_id
    )


def _proposal_fingerprint(proposal: ActionProposal) -> str | None:
    """Normalized tool/target/args fingerprint for direct bindings."""
    tool_id = getattr(proposal, "tool_id", None)
    if not _is_direct_binding(getattr(proposal, "binding_mode", "legacy_action"), tool_id):
        return None
    try:
        return _binding_fingerprint(tool_id, proposal.target, proposal.arguments)
    except Exception:
        return None


def _binding_fingerprint_matches(first: ActionProposal, second: ActionProposal) -> bool:
    """Compare normalized tool/target/args for direct bindings."""
    first_direct = _is_direct_binding(
        getattr(first, "binding_mode", "legacy_action"), getattr(first, "tool_id", None)
    )
    second_direct = _is_direct_binding(
        getattr(second, "binding_mode", "legacy_action"), getattr(second, "tool_id", None)
    )
    if not first_direct and not second_direct:
        return True
    if first_direct != second_direct:
        return False
    if getattr(first, "tool_id", None) != getattr(second, "tool_id", None):
        return False
    try:
        return _binding_fingerprint(
            getattr(first, "tool_id", None), first.target, first.arguments
        ) == _binding_fingerprint(
            getattr(second, "tool_id", None), second.target, second.arguments
        )
    except Exception:
        return False


def _stale_proposal_decision(action_id, approval_id: ApprovalId | str) -> PolicyDecision:
    return PolicyDecision(
        action_id=action_id,
        verdict=PolicyVerdict.DENY,
        reasons=[
            f"stale approval {approval_id}: current proposal no longer "
            f"matches the approved content -- cannot safely resume"
        ],
        matched_rules=["approval", "stale", to_tag(Severity.ABORT_RUN)],
        approval_id=ApprovalId(str(approval_id)),
        evaluated_at=datetime.now(timezone.utc),
    )


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
        outbound_descriptor_resolver: Callable[[ToolId], ToolDescriptor | None] | None = None,
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
        # Optional outbound-text narrowing: resolves the actually selected
        # tool (ControlContext.selected_tool_id, supplied by root policy and
        # execute paths) to its declared outbound_text_fields. None means
        # unknown binding: no batch outbound reject here (unavailable-tool
        # diagnosis applies elsewhere); declared empty means nothing to check.
        self._outbound_descriptor_resolver = outbound_descriptor_resolver
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
        # Retains the approved proposal behind a resolved ALLOW so a later
        # approval_outcome() with current_proposal can still prove the same
        # tool/target/arguments binding. Popped proposals alone cannot
        # validate a cache hit; without this, a changed call could reuse a
        # cached ALLOW the human never saw.
        self._resolved_proposals: dict[ApprovalId, ActionProposal] = {}
        # Root review: admission never caches time checks. Receipts are
        # caller correlation only. Retries track consumed attempts by key,
        # never by replaying a stored decision before current checks.
        self._consumed_reservations: set[tuple[str, str, str, int]] = set()

    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision:
        engagement_id = context.engagement_id
        engagement = self._engagements[engagement_id]

        # Outbound-text gate first: a blocked input is denied before any
        # proposal original reaches durable audit or blocked-action evidence.
        # Only a cleaned copy (empty arguments) plus safe reason codes is stored.
        outbound_descriptor = self._resolve_outbound_descriptor(context)
        outbound_blocked, outbound_reasons, _outbound_code = evaluate_with_descriptor(
            proposal, outbound_descriptor, engagement.external_text_policy
        )
        if outbound_blocked:
            # Outbound DENY: _finalize stores a cleaned copy plus safe codes only.
            return await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                outbound_reasons,
                [OUTBOUND_MATCHED_RULE],
                severity=Severity.BLOCK_ACTION,
            )

        scope_ok, scope_reasons, scope_severity = check_scope(proposal, engagement.scope)
        if not scope_ok:
            return await self._finalize(
                proposal, PolicyVerdict.DENY, scope_reasons, ["scope"], severity=scope_severity
            )

        # Selected-tool direct inspection only: the selected descriptor's own
        # risk, never the max over every tool serving the action. An unknown
        # selected binding escalates to a human (no legacy max fallback).
        # Legacy callers without a selected tool keep the registry behavior.
        # Resolved once here so ROE, target-action and policy stages share it.
        selected_descriptor = outbound_descriptor
        has_selected_tool = getattr(context, "selected_tool_id", None) is not None
        in_flight = self._in_flight_from_context(context)
        try:
            roe_ok, roe_reasons, roe_severity = check_roe(
                proposal,
                engagement,
                in_flight_actions=in_flight,
                selected=selected_descriptor,
                has_selected_tool=has_selected_tool,
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

        target_ok, target_reasons, target_severity = check_target_action_policies(
            proposal,
            engagement.target_action_policies,
            selected=selected_descriptor,
            has_selected_tool=has_selected_tool,
        )
        if not target_ok:
            return await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                target_reasons,
                ["target_action_policy"],
                severity=target_severity,
            )

        approval_policy = engagement.approval_policy
        if has_selected_tool:
            if selected_descriptor is not None:
                risk, risk_unresolved = selected_descriptor.risk_level, False
            else:
                risk, risk_unresolved = None, True
        else:
            risk, risk_unresolved = self._resolve_risk(proposal)
        verdict, policy_reasons, matched_rules = evaluate_policy(
            proposal,
            approval_policy,
            risk=risk,
            # Engagement-configured threshold, else the Engine default.
            risk_threshold=resolve_risk_threshold(approval_policy),
            selected=selected_descriptor,
            has_selected_tool=has_selected_tool,
        )
        if risk_unresolved and verdict != PolicyVerdict.DENY:
            # Manual escalation, not a fail-closed DENY: risk-tiered
            # approval exists precisely to catch risky actions, so when
            # the risk lookup errors we escalate to a human rather than
            # let a possibly-risky action through on an unresolved risk.
            # A human can still approve it -- this stage only refuses to
            # decide ALLOW on its own when it doesn't know the risk.
            verdict = PolicyVerdict.REQUIRE_APPROVAL
            policy_reasons = [
                *policy_reasons,
                "tool risk_level could not be resolved — requiring approval",
            ]
            matched_rules = [*matched_rules, "risk:unresolved"]
        if verdict == PolicyVerdict.DENY:
            # A policy deny rejects this class of action — switch goals.
            return await self._finalize(
                proposal, verdict, policy_reasons, matched_rules, severity=Severity.REPLAN_GOAL
            )

        unavailable = unavailable_restriction_reasons(
            proposal,
            engagement,
            selected=selected_descriptor,
            has_selected_tool=has_selected_tool,
        )
        if unavailable:
            # Explicit restrictions with no provider declaration to
            # evaluate: never silently pass. A firing meaning here always
            # needs a human (existing human_required handling below
            # bypasses auto-approval ceilings for this rule).
            verdict = PolicyVerdict.REQUIRE_APPROVAL
            policy_reasons = [*policy_reasons, *unavailable]
            if POLICY_SEMANTICS_UNAVAILABLE not in matched_rules:
                matched_rules = [*matched_rules, POLICY_SEMANTICS_UNAVAILABLE]

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

        budget = self._budgets.setdefault(engagement_id, BudgetTracker(engagement.execution_budget))
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
        # A command-pattern or semantics-unavailable verdict always needs a
        # human: it bypasses auto-approval ceilings by meaning, not by level.
        human_required = any(
            rule == POLICY_SEMANTICS_UNAVAILABLE or rule.startswith(COMMAND_MATCH_PREFIX)
            for rule in matched_rules
        )
        if requires_approval and (human_required or not may_auto_approve(approval_policy, risk)):
            fingerprint = _proposal_fingerprint(proposal)
            approval = await self._approvals.request(
                proposal.action_id, execution_fingerprint=fingerprint
            )
            self._pending_proposals[approval.approval_id] = proposal.model_copy(deep=True)
            decision = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reasons=policy_reasons,
                matched_rules=matched_rules,
                approval_id=approval.approval_id,
                rate_limit_remaining=rate_limiter.remaining,
                budget_remaining=budget.remaining,
                evaluated_at=datetime.now(timezone.utc),
                human_required=human_required,
                execution_fingerprint=fingerprint,
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
        expected_fp = getattr(approval, "execution_fingerprint", None)
        candidate_fp = _proposal_fingerprint(proposal)
        if expected_fp is None:
            if candidate_fp is not None:
                raise ValueError(
                    f"approval {normalized_id} carries no execution fingerprint; "
                    f"refusing to link a direct selected-tool proposal"
                )
        elif candidate_fp != expected_fp:
            raise ValueError(
                f"approval {normalized_id} fingerprint does not match proposal "
                f"{proposal.action_id}; refusing to link"
            )
        self._pending_proposals[normalized_id] = proposal.model_copy(deep=True)
        return approval

    async def approval_outcome(
        self, approval_id: ApprovalId | str, current_proposal: ActionProposal | None = None
    ) -> PolicyDecision | None:
        normalized_id = ApprovalId(str(approval_id))
        cached = self._resolved_outcomes.get(normalized_id)
        if cached is not None:
            if current_proposal is None:
                return cached
            original = self._resolved_proposals.get(normalized_id)
            if original is None:
                if cached.verdict is not PolicyVerdict.ALLOW:
                    return cached
                return _stale_proposal_decision(cached.action_id, normalized_id)
            if cached.action_id != current_proposal.action_id:
                return _stale_proposal_decision(cached.action_id, normalized_id)
            cached_fp = getattr(cached, "execution_fingerprint", None)
            if cached_fp is not None:
                if _proposal_fingerprint(current_proposal) != cached_fp:
                    return _stale_proposal_decision(cached.action_id, normalized_id)
            if not _same_action_content(original, current_proposal):
                return _stale_proposal_decision(cached.action_id, normalized_id)
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

        stored_fp = _proposal_fingerprint(proposal)
        expected_fp = getattr(approval, "execution_fingerprint", None)
        if expected_fp is None:
            if stored_fp is not None:
                decision = _stale_proposal_decision(action_id, normalized_id)
                self._resolved_outcomes[normalized_id] = decision
                return decision
        elif stored_fp != expected_fp:
            decision = _stale_proposal_decision(action_id, normalized_id)
            self._resolved_outcomes[normalized_id] = decision
            return decision
        if current_proposal is not None and expected_fp is not None:
            if _proposal_fingerprint(current_proposal) != expected_fp:
                decision = _stale_proposal_decision(action_id, normalized_id)
                self._resolved_outcomes[normalized_id] = decision
                return decision
        if current_proposal is not None:
            if current_proposal.action_id != proposal.action_id:
                decision = _stale_proposal_decision(action_id, normalized_id)
                self._resolved_outcomes[normalized_id] = decision
                return decision
            if not _same_action_content(proposal, current_proposal):
                decision = _stale_proposal_decision(action_id, normalized_id)
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
        self._resolved_proposals[normalized_id] = proposal.model_copy(deep=True)
        self._resolved_outcomes[normalized_id] = decision
        return decision

    async def describe_policy(self, engagement_id: EngagementId | str) -> dict[str, Any]:
        normalized = EngagementId(str(engagement_id))
        engagement = self._engagements.get(normalized)
        if engagement is None:
            raise KeyError(f"unregistered engagement: {engagement_id}")
        approval_policy = engagement.approval_policy
        roe_rules = list(engagement.rules_of_engagement or [])
        time_gated = any(
            r.start_at is not None or r.end_at is not None or r.allowed_time_windows
            for r in roe_rules
        )
        max_parallel = any(r.max_parallel_actions is not None for r in roe_rules)
        availability_gated = any(not r.availability_impact_allowed for r in roe_rules) if roe_rules else False
        has_allow_lists = any(r.allowed_capabilities or r.allowed_operations or r.allowed_time_windows for r in roe_rules)
        ceiling = auto_approval_ceiling(approval_policy)
        threshold = resolve_risk_threshold(approval_policy)
        command_label = COMMAND_MATCH_PREFIX[:-1] if COMMAND_MATCH_PREFIX.endswith(":") else COMMAND_MATCH_PREFIX
        explicit_command_rules = list(
            getattr(approval_policy, "command_approval_rules", None) or []
        )
        command_source = "explicit" if explicit_command_rules else "default"
        return {
            "engagement_id": str(normalized),
            "scope_enforced": True,
            "empty_allow_lists": {
                "rules_of_engagement": "no_additional_restriction",
                "target_action_policy": "deny_all_when_explicit_empty",
            },
            "roe": {
                "has_rules": bool(roe_rules),
                "has_allow_lists": bool(has_allow_lists),
                "time_gated": bool(time_gated),
                "availability_gated": bool(availability_gated),
                "max_parallel_configured": bool(max_parallel),
            },
            "approval": {
                "explicit_approval_configured": bool(approval_policy.require_approval_for),
                "command_rules_configured": bool(explicit_command_rules),
                "command_rule_source": command_source,
                "command_default_minimum_risk": (
                    next(
                        (
                            rule.minimum_risk.value
                            for rule in DEFAULT_COMMAND_APPROVAL_RULES
                            if rule.minimum_risk is not None
                        ),
                        None,
                    )
                    if command_source == "default"
                    else None
                ),
                "risk_threshold": threshold.value if threshold is not None else None,
                "auto_approve_ceiling": ceiling.value if ceiling is not None else None,
                "human_required_on": [command_label, POLICY_SEMANTICS_UNAVAILABLE],
            },
            "target_action_policies_configured": bool(engagement.target_action_policies),
        }

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
                raise ValueError(f"elapsed_duration_seconds must be finite, got {elapsed!r}")
            if elapsed < 0:
                raise ValueError(f"elapsed_duration_seconds must be non-negative, got {elapsed}")
            max_duration = engagement.execution_budget.max_duration_seconds
            if max_duration is not None:
                elapsed = min(elapsed, float(max_duration))

        budget = self._budgets.setdefault(normalized_id, BudgetTracker(engagement.execution_budget))
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

    async def record_tool_calls(self, engagement_id: EngagementId | str, count: int = 1) -> int:
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
        budget = self._budgets.setdefault(normalized_id, BudgetTracker(engagement.execution_budget))
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

    def _recheck_time_gates(
        self, proposal, engagement, *, now_utc, in_flight, selected=None, has_selected_tool=False
    ):
        from redteam.control.roe import UnsupportedRoePolicyError

        try:
            roe_ok, roe_reasons, roe_severity = check_roe(
                proposal,
                engagement,
                now=now_utc,
                in_flight_actions=in_flight,
                selected=selected,
                has_selected_tool=has_selected_tool,
            )
        except UnsupportedRoePolicyError as exc:
            return False, "roe_window_closed", [str(exc)], Severity.ABORT_RUN
        if roe_ok:
            return True, "", [], None
        joined = " ".join(roe_reasons).lower()
        if "max_parallel" in joined or "in flight" in joined or "in-flight" in joined:
            return (
                False,
                "roe_parallel_exceeded",
                ["roe_parallel_exceeded", *roe_reasons],
                roe_severity,
            )
        if "availability" in joined:
            return (
                False,
                "availability_blocked",
                ["availability_blocked", *roe_reasons],
                roe_severity,
            )
        return False, "roe_window_closed", ["roe_window_closed", *roe_reasons], roe_severity

    @staticmethod
    def _in_flight_from_context(context: ControlContext):
        typed = getattr(context, "in_flight_actions", None)
        if typed is not None:
            return typed
        try:
            meta = (context.metadata or {}).get("in_flight_actions")
        except Exception:
            return None
        return meta

    async def _current_admission_check(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
        attempt_index=None,
    ):
        now_utc = datetime.now(timezone.utc)
        if receipt_id is None or not str(receipt_id):
            return (
                False,
                "decision_mismatch",
                ["missing receipt"],
                ["admission"],
                Severity.BLOCK_ACTION,
                None,
                None,
                now_utc,
                None,
            )
        idx = None
        if attempt_index is not None:
            try:
                idx = int(attempt_index)
            except Exception:
                return (
                    False,
                    "decision_mismatch",
                    ["invalid attempt index"],
                    ["admission"],
                    Severity.BLOCK_ACTION,
                    None,
                    None,
                    now_utc,
                    None,
                )
            if idx < 1:
                return (
                    False,
                    "decision_mismatch",
                    ["attempt_index must be >= 1"],
                    ["admission"],
                    Severity.BLOCK_ACTION,
                    None,
                    None,
                    now_utc,
                    None,
                )
        if decision.action_id != proposal.action_id or decision.verdict != PolicyVerdict.ALLOW:
            return (
                False,
                "decision_mismatch",
                ["admission requires ALLOW for this action"],
                ["admission"],
                Severity.BLOCK_ACTION,
                None,
                None,
                now_utc,
                idx,
            )
        if proposal.engagement_id != context.engagement_id:
            return (
                False,
                "decision_mismatch",
                ["proposal engagement does not match context"],
                ["admission"],
                Severity.BLOCK_ACTION,
                None,
                None,
                now_utc,
                idx,
            )
        try:
            meta_run = (context.metadata or {}).get("run_id")
        except Exception:
            meta_run = None
        if meta_run is not None and str(meta_run) != str(proposal.run_id):
            return (
                False,
                "decision_mismatch",
                ["proposal run does not match context"],
                ["admission"],
                Severity.BLOCK_ACTION,
                None,
                None,
                now_utc,
                idx,
            )
        if decision.approval_id is not None:
            approval = await self._approvals.get(decision.approval_id)
            if (
                approval is None
                or approval.action_id != proposal.action_id
                or approval.status != ApprovalStatus.APPROVED
            ):
                return (
                    False,
                    "decision_mismatch",
                    ["approval is not APPROVED for this action"],
                    ["admission", "approval"],
                    Severity.BLOCK_ACTION,
                    None,
                    None,
                    now_utc,
                    idx,
                )
        has_valid_approval = decision.approval_id is not None
        engagement = self._engagements.get(context.engagement_id)
        if engagement is None:
            return (
                False,
                "decision_mismatch",
                ["unknown engagement"],
                ["admission"],
                Severity.BLOCK_ACTION,
                None,
                None,
                now_utc,
                idx,
            )
        # Same selected-tool meaning as evaluate(): the descriptor behind
        # the call, so explicit restrictions reuse the provider-declared
        # keys instead of blank capability/operation strings.
        selected_descriptor = self._resolve_outbound_descriptor(context)
        has_selected = getattr(context, "selected_tool_id", None) is not None
        scope_ok, scope_reasons, scope_severity = check_scope(proposal, engagement.scope)
        if not scope_ok:
            return (
                False,
                "scope_revoked",
                list(scope_reasons),
                ["scope"],
                scope_severity,
                engagement,
                self._budgets.get(context.engagement_id),
                now_utc,
                idx,
            )
        in_flight = self._in_flight_from_context(context)
        now_utc = datetime.now(timezone.utc)
        time_ok, time_code, time_reasons, time_severity = self._recheck_time_gates(
            proposal,
            engagement,
            now_utc=now_utc,
            in_flight=in_flight,
            selected=selected_descriptor,
            has_selected_tool=has_selected,
        )
        if not time_ok:
            base = (
                [r for r in time_reasons[1:] if r]
                if time_reasons and time_reasons[0] == time_code
                else list(time_reasons)
            )
            return (
                False,
                time_code,
                base,
                ["roe"],
                time_severity,
                engagement,
                self._budgets.get(context.engagement_id),
                now_utc,
                idx,
            )
        target_ok, target_reasons, target_severity = check_target_action_policies(
            proposal,
            engagement.target_action_policies,
            selected=selected_descriptor,
            has_selected_tool=has_selected,
        )
        if not target_ok:
            return (
                False,
                "target_policy_denied",
                list(target_reasons),
                ["target_action_policy"],
                target_severity,
                engagement,
                self._budgets.get(context.engagement_id),
                now_utc,
                idx,
            )
        unavailable = unavailable_restriction_reasons(
            proposal,
            engagement,
            selected=selected_descriptor,
            has_selected_tool=has_selected,
        )
        if unavailable and not has_valid_approval:
            # No approved human decision covers this call, so an
            # unevaluable explicit restriction fails closed here. With a
            # valid APPROVED approval the human already accepted it at
            # evaluate time (same meaning), and only current gates below
            # still apply.
            return (
                False,
                "policy_semantics_unavailable",
                list(unavailable),
                ["policy"],
                Severity.BLOCK_ACTION,
                engagement,
                self._budgets.get(context.engagement_id),
                now_utc,
                idx,
            )
        outbound_descriptor = selected_descriptor
        outbound_blocked, outbound_reasons, _outbound_code = evaluate_with_descriptor(
            proposal, outbound_descriptor, engagement.external_text_policy
        )
        if outbound_blocked:
            return (
                False,
                "outbound_blocked",
                list(outbound_reasons),
                [OUTBOUND_MATCHED_RULE],
                Severity.BLOCK_ACTION,
                engagement,
                self._budgets.get(context.engagement_id),
                now_utc,
                idx,
            )
        budget = self._budgets.get(context.engagement_id)
        if budget is not None and budget.duration_exhausted():
            return (
                False,
                "duration_exhausted",
                ["execution duration budget is exhausted"],
                ["budget"],
                Severity.ABORT_RUN,
                engagement,
                budget,
                now_utc,
                idx,
            )
        return (True, "", [], [], None, engagement, budget, now_utc, idx)

    async def admit_invocation(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
    ) -> PolicyDecision:
        """Re-check scope, ROE time and duration without charging.

        Time checks are fresh; receipt_id is caller correlation only.
        """
        (
            ok,
            code,
            extra,
            matched,
            severity,
            _eng,
            _budget,
            now_utc,
            _idx,
        ) = await self._current_admission_check(proposal, decision, context, receipt_id=receipt_id)
        if not ok:
            return await self._finalize(
                proposal,
                PolicyVerdict.DENY,
                [code, *extra],
                matched,
                severity=severity,
                approval_id=decision.approval_id,
            )
        allowed = PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.ALLOW,
            reasons=["admitted"],
            matched_rules=list(decision.matched_rules),
            approval_id=decision.approval_id,
            rate_limit_remaining=decision.rate_limit_remaining,
            budget_remaining=decision.budget_remaining,
            evaluated_at=now_utc,
            severity=None,
            execution_fingerprint=decision.execution_fingerprint,
        )
        return allowed

    async def reserve_extra_tool_call(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
        attempt_index: int,
    ) -> PolicyDecision:
        """Reserve an extra retry call without charging rate or action counters.

        Current checks precede the consumed-key check, capacity and one charge.
        Decisions are never stored or replayed here.
        """

        def _severity_for(code: str) -> Severity:
            if code in ("duration_exhausted", "roe_window_closed"):
                return Severity.ABORT_RUN
            return Severity.BLOCK_ACTION

        def _deny_plain(code: str, detail: str) -> PolicyDecision:
            sev = _severity_for(code)
            base = (
                "budget"
                if code in ("tool_calls_exhausted", "duration_exhausted")
                else ("admission" if code == "decision_mismatch" else "roe")
            )
            denied = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.DENY,
                reasons=[code, detail] if detail else [code],
                matched_rules=[base, to_tag(sev)],
                approval_id=decision.approval_id,
                evaluated_at=datetime.now(timezone.utc),
                severity=sev,
            )
            return denied

        # A retry re-invokes the already-admitted action tool; it does not
        # create a new parallel slot of its own. Concurrency is still checked
        # here against the live others count from context (typed first, legacy
        # metadata fallback), so a genuinely saturated parallel gate still
        # denies instead of being bypassed.
        (
            ok,
            code,
            extra,
            matched,
            severity,
            _eng,
            budget,
            now_utc,
            idx,
        ) = await self._current_admission_check(
            proposal, decision, context, receipt_id=receipt_id, attempt_index=attempt_index
        )
        if not ok:
            detail = extra[0] if extra else ""
            base = (
                "budget"
                if code in ("tool_calls_exhausted", "duration_exhausted")
                else (
                    "admission"
                    if code == "decision_mismatch"
                    else (
                        "scope"
                        if code == "scope_revoked"
                        else (
                            "target_action_policy"
                            if code == "target_policy_denied"
                            else ("outbound_text" if code == "outbound_blocked" else "roe")
                        )
                    )
                )
            )
            sev = severity if severity is not None else _severity_for(code)
            denied = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.DENY,
                reasons=[code, detail] if detail else [code],
                matched_rules=[base, to_tag(sev)],
                approval_id=decision.approval_id,
                evaluated_at=now_utc,
                severity=sev,
            )
            return denied
        assert idx is not None
        consumed_key = (
            str(context.engagement_id),
            str(proposal.action_id),
            str(receipt_id),
            int(idx),
        )
        if consumed_key in self._consumed_reservations:
            allowed = PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.ALLOW,
                reasons=["extra_tool_call_reserved"],
                matched_rules=list(decision.matched_rules),
                approval_id=decision.approval_id,
                rate_limit_remaining=decision.rate_limit_remaining,
                budget_remaining=(budget.remaining if budget is not None else None),
                evaluated_at=now_utc,
                severity=None,
                execution_fingerprint=decision.execution_fingerprint,
            )
            return allowed
        if budget is None:
            return _deny_plain("tool_calls_exhausted", "no budget tracker for this engagement")
        if budget.tool_calls_remaining <= 0:
            return _deny_plain("tool_calls_exhausted", "tool call budget is exhausted")
        budget.consume_tool_calls(1)
        self._consumed_reservations.add(consumed_key)
        allowed = PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.ALLOW,
            reasons=["extra_tool_call_reserved"],
            matched_rules=list(decision.matched_rules),
            approval_id=decision.approval_id,
            rate_limit_remaining=decision.rate_limit_remaining,
            budget_remaining=budget.remaining,
            evaluated_at=now_utc,
            severity=None,
            execution_fingerprint=decision.execution_fingerprint,
        )
        return allowed

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
        # Dual-write severity: typed PolicyDecision.severity plus the legacy
        # matched_rules tag. Tag readers must use
        # core.contracts.policy.legacy_denial_severity (or the
        # PolicyDecision.denial_severity property), never a new parser here.
        if verdict is PolicyVerdict.DENY and OUTBOUND_MATCHED_RULE in matched_rules:
            # Outbound-text deny: never persist original arguments anywhere.
            # Audit and blocked-action evidence see only a cleaned copy plus
            # safe reason codes (no input text, values, or pattern contents).
            proposal = proposal.model_copy(update={"arguments": {}})
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
            severity=severity,
            execution_fingerprint=_proposal_fingerprint(proposal),
        )
        await self._audit.record(proposal, decision)
        await self._record_blocked_action(proposal, decision)
        return decision

    def _resolve_outbound_descriptor(self, context: ControlContext) -> ToolDescriptor | None:
        tool_id = getattr(context, "selected_tool_id", None)
        if tool_id is None or self._outbound_descriptor_resolver is None:
            return None
        try:
            return self._outbound_descriptor_resolver(ToolId(str(tool_id)))
        except Exception:
            logger.exception("outbound descriptor lookup failed; treating as unknown")
            return None

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
