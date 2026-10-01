"""Prompt 6 safety accounting: restart budget/rate, approval rate charge,
and the bounded-retry budget seam.

Three defects this pins down:

1. A rebuilt ``DefaultControlPlane`` (durable resume) starts with empty
   budget/rate counters, so it would hand the resumed run a fresh full
   budget. ``rehydrate`` restores the original run's counters, bounded and
   monotonic.
2. A retry re-invokes a tool but used to bypass Control's authoritative
   budget. ``record_tool_calls`` is the seam that debits retries so
   ``max_tool_calls`` still binds.
3. ``approval_outcome`` used to charge a rate slot *twice* (once at the
   original ``REQUIRE_APPROVAL`` evaluation, again on approval). A rate slot
   and a budget action are now charged exactly once per admitted action.

Targeted unit scope only -- no graph/E2E execution here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.control.budget import BudgetTracker
from redteam.control.rate_limit import RateLimiter
from redteam.control.service import DefaultControlPlane, RehydratedAccounting
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId

ENGAGEMENT_ID = EngagementId("eng_acct")
RUN_ID = RunId("run_acct")


def _engagement(
    *,
    max_actions: int = 5,
    max_tool_calls: int | None = None,
    per_minute: int = 30,
    approval: ApprovalPolicy | None = None,
) -> Engagement:
    return Engagement(
        engagement_id=ENGAGEMENT_ID,
        name="accounting",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(
            max_actions=max_actions, max_tool_calls=max_tool_calls
        ),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=per_minute),
        approval_policy=approval or ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(action_id: str = "action_1", *, operation: str = "port_scan") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RUN_ID,
        engagement_id=ENGAGEMENT_ID,
        agent_id=AgentId("agent_acct"),
        capability="recon",
        operation=operation,
        target=HostTarget(host="good.local"),
        reason="accounting test",
        created_at=datetime.now(timezone.utc),
    )


def _plane(engagement: Engagement) -> DefaultControlPlane:
    return DefaultControlPlane({engagement.engagement_id: engagement})


async def _evaluate(
    plane: DefaultControlPlane, action_id: str = "action_1", *, operation: str = "port_scan"
):
    return await plane.evaluate(
        _proposal(action_id, operation=operation),
        ControlContext(engagement_id=ENGAGEMENT_ID),
    )


# --- BudgetTracker.rehydrate / consume_tool_calls ----------------------


def test_budget_rehydrate_restores_consumed_and_remaining() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5))
    tracker.rehydrate(actions_consumed=3)

    assert tracker.consumed == 3
    assert tracker.remaining == 2
    assert tracker.has_capacity() is True
    tracker.consume()
    tracker.consume()
    assert tracker.has_capacity() is False


def test_budget_rehydrate_clamps_an_over_cap_count() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=2))
    tracker.rehydrate(actions_consumed=999)

    assert tracker.consumed == 2
    assert tracker.remaining == 0
    assert tracker.has_capacity() is False


def test_budget_rehydrate_is_monotonic() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5))
    tracker.rehydrate(actions_consumed=3)
    tracker.rehydrate(actions_consumed=1)  # cannot hand capacity back
    assert tracker.consumed == 3


def test_budget_rehydrate_rejects_negative_counts() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5))
    with pytest.raises(ValueError):
        tracker.rehydrate(actions_consumed=-1)
    with pytest.raises(ValueError):
        tracker.rehydrate(actions_consumed=1, tool_calls_consumed=-1)


def test_budget_rehydrate_respects_the_tool_call_cap() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=10, max_tool_calls=2))
    tracker.rehydrate(actions_consumed=1, tool_calls_consumed=2)

    assert tracker.consumed == 1
    assert tracker.tool_calls_consumed == 2
    assert tracker.has_capacity() is False
    assert tracker.remaining == 0


def test_budget_rehydrate_defaults_tool_calls_to_actions() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5))
    tracker.rehydrate(actions_consumed=3)
    assert tracker.tool_calls_consumed == 3


def test_consume_tool_calls_debits_only_the_tool_call_counter() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5, max_tool_calls=3))
    tracker.consume()
    tracker.consume_tool_calls(2)

    assert tracker.consumed == 1  # one admitted action
    assert tracker.tool_calls_consumed == 3
    assert tracker.has_capacity() is False

    with pytest.raises(ValueError):
        tracker.consume_tool_calls(-1)


# --- RateLimiter.would_allow / rehydrate -------------------------------


def test_would_allow_checks_without_consuming() -> None:
    limiter = RateLimiter(RateLimitPolicy(max_actions_per_minute=2))
    assert limiter.would_allow(now=0.0) is True
    assert limiter.would_allow(now=0.0) is True
    assert limiter.in_window == 0
    assert limiter.remaining == 2


def test_rate_rehydrate_restores_window_conservatively() -> None:
    limiter = RateLimiter(RateLimitPolicy(max_actions_per_minute=3))
    limiter.rehydrate(1, now=0.0)
    assert limiter.in_window == 1
    assert limiter.remaining == 2

    full = RateLimiter(RateLimitPolicy(max_actions_per_minute=2))
    full.rehydrate(2, now=0.0)
    assert full.allow(now=1.0) is False


def test_rate_rehydrate_is_monotonic_and_capped() -> None:
    limiter = RateLimiter(RateLimitPolicy(max_actions_per_minute=2))
    limiter.rehydrate(5, now=0.0)  # capped at the per-minute limit
    assert limiter.in_window == 2
    limiter.rehydrate(0, now=0.0)  # cannot free slots
    assert limiter.in_window == 2

    with pytest.raises(ValueError):
        limiter.rehydrate(-1)


# --- restart: DefaultControlPlane.rehydrate ----------------------------


async def test_without_rehydrate_a_fresh_plane_grants_a_fresh_budget() -> None:
    """Documents the defect rehydrate fixes: a rebuilt plane forgets spend."""
    engagement = _engagement(max_actions=2)
    first = _plane(engagement)
    assert (await _evaluate(first, "action_1")).verdict == PolicyVerdict.ALLOW
    assert (await _evaluate(first, "action_2")).verdict == PolicyVerdict.ALLOW

    rebuilt = _plane(engagement)
    assert (await _evaluate(rebuilt, "action_3")).verdict == PolicyVerdict.ALLOW


async def test_rehydrate_restores_spent_budget_across_restart() -> None:
    engagement = _engagement(max_actions=2)
    first = _plane(engagement)
    assert (await _evaluate(first, "action_1")).verdict == PolicyVerdict.ALLOW
    assert (await _evaluate(first, "action_2")).verdict == PolicyVerdict.ALLOW

    rebuilt = _plane(engagement)
    restored = await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=2
    )

    assert isinstance(restored, RehydratedAccounting)
    assert restored.actions_consumed == 2
    assert restored.tool_calls_consumed == 2
    assert restored.budget_remaining == 0

    decision = await _evaluate(rebuilt, "action_3")
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules


async def test_rehydrate_restores_rate_window_conservatively() -> None:
    engagement = _engagement(max_actions=10, per_minute=1)
    rebuilt = _plane(engagement)

    restored = await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=1
    )
    assert restored.rate_actions_in_window == 1
    assert restored.rate_remaining == 0

    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.DENY
    assert "rate_limit" in decision.matched_rules


async def test_rehydrate_is_monotonic_across_repeated_calls() -> None:
    engagement = _engagement(max_actions=2)
    rebuilt = _plane(engagement)

    await rebuilt.rehydrate(engagement.engagement_id, actions_consumed=2)
    await rebuilt.rehydrate(engagement.engagement_id, actions_consumed=0)

    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules


async def test_rehydrate_clamps_a_corrupt_overcount() -> None:
    engagement = _engagement(max_actions=2)
    rebuilt = _plane(engagement)

    restored = await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=999
    )
    assert restored.actions_consumed == 2
    assert restored.budget_remaining == 0


async def test_rehydrate_rejects_unknown_engagement_and_absurd_counts() -> None:
    engagement = _engagement()
    rebuilt = _plane(engagement)

    with pytest.raises(KeyError, match="unregistered engagement"):
        await rebuilt.rehydrate("eng_missing", actions_consumed=1)

    with pytest.raises(ValueError):
        await rebuilt.rehydrate(engagement.engagement_id, actions_consumed=-1)

    with pytest.raises(ValueError, match="maximum rehydratable"):
        await rebuilt.rehydrate(
            engagement.engagement_id, actions_consumed=2_000_000
        )


# --- approval lifecycle: charge exactly once ---------------------------


def _approval_engagement(**overrides) -> Engagement:
    approval = ApprovalPolicy(require_approval_for=["recon.port_scan"])
    return _engagement(approval=approval, **overrides)


async def test_approval_outcome_charges_rate_exactly_once() -> None:
    # cap = 1: if the original REQUIRE_APPROVAL evaluation charged a slot,
    # the later approval would be denied by rate limit. It must allow.
    engagement = _approval_engagement(per_minute=1)
    plane = _plane(engagement)

    pending = await _evaluate(plane)
    assert pending.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert pending.approval_id is not None

    await plane.resolve_approval(pending.approval_id, ApprovalStatus.APPROVED, "operator")
    approved = await plane.approval_outcome(pending.approval_id)
    assert approved is not None
    assert approved.verdict == PolicyVerdict.ALLOW

    # The single slot is now used: a further action is rate limited.
    next_decision = await _evaluate(plane, "action_2", operation="service_discovery")
    assert next_decision.verdict == PolicyVerdict.DENY
    assert "rate_limit" in next_decision.matched_rules


async def test_rejected_approval_never_consumes_a_rate_slot() -> None:
    engagement = _approval_engagement(per_minute=1)
    plane = _plane(engagement)

    pending = await _evaluate(plane)
    assert pending.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert pending.approval_id is not None

    await plane.resolve_approval(pending.approval_id, ApprovalStatus.REJECTED, "operator")
    rejected = await plane.approval_outcome(pending.approval_id)
    assert rejected is not None
    assert rejected.verdict == PolicyVerdict.DENY

    # Nothing used the only rate slot, so a fresh action still fits.
    allowed = await _evaluate(plane, "action_2", operation="service_discovery")
    assert allowed.verdict == PolicyVerdict.ALLOW


async def test_approval_outcome_charges_budget_once_and_is_idempotent() -> None:
    engagement = _approval_engagement(max_actions=2, per_minute=30)
    plane = _plane(engagement)

    pending = await _evaluate(plane)
    assert pending.approval_id is not None
    await plane.resolve_approval(pending.approval_id, ApprovalStatus.APPROVED, "operator")

    first = await plane.approval_outcome(pending.approval_id)
    second = await plane.approval_outcome(pending.approval_id)
    assert first is not None and first.verdict == PolicyVerdict.ALLOW
    assert second is first  # cached: no second consume

    # Exactly one of the two action slots was charged.
    second_action = await _evaluate(plane, "action_2", operation="service_discovery")
    assert second_action.verdict == PolicyVerdict.ALLOW
    exhausted = await _evaluate(plane, "action_3", operation="service_discovery")
    assert exhausted.verdict == PolicyVerdict.DENY
    assert "budget" in exhausted.matched_rules


# --- bounded retries: record_tool_calls seam ---------------------------


async def test_record_tool_calls_makes_control_budget_authoritative() -> None:
    engagement = _engagement(max_actions=10, max_tool_calls=2)
    plane = _plane(engagement)

    admitted = await _evaluate(plane, "action_1")
    assert admitted.verdict == PolicyVerdict.ALLOW
    assert admitted.budget_remaining == 1
    assert await plane.has_tool_call_capacity(engagement.engagement_id) is True

    remaining = await plane.record_tool_calls(engagement.engagement_id, 1)
    assert remaining == 0
    # The retry counter is at its cap: a further retry must be refused
    # *before* it re-invokes the tool.
    assert await plane.has_tool_call_capacity(engagement.engagement_id) is False

    # The retry pushed the authoritative tool-call counter to its cap, so
    # no further action is admitted even though only one action ran.
    blocked = await _evaluate(plane, "action_2")
    assert blocked.verdict == PolicyVerdict.DENY
    assert "budget" in blocked.matched_rules


async def test_record_tool_calls_validates_its_inputs() -> None:
    engagement = _engagement()
    plane = _plane(engagement)

    with pytest.raises(KeyError, match="unregistered engagement"):
        await plane.record_tool_calls("eng_missing", 1)
    with pytest.raises(ValueError):
        await plane.record_tool_calls(engagement.engagement_id, -1)

    # No action admitted yet -> a fresh tracker has full tool-call capacity.
    assert await plane.has_tool_call_capacity(engagement.engagement_id) is True
    with pytest.raises(KeyError, match="unregistered engagement"):
        await plane.has_tool_call_capacity("eng_missing")


async def test_rehydrate_restores_retry_tool_calls_too() -> None:
    # A run that spent one action plus one retry: restoring both counters
    # leaves no tool-call headroom and the run is refused.
    engagement = _engagement(max_actions=10, max_tool_calls=2)
    rebuilt = _plane(engagement)

    restored = await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=1, tool_calls_consumed=2
    )
    assert restored.tool_calls_consumed == 2
    assert restored.budget_remaining == 0

    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules
