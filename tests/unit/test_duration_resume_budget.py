"""Duration budget across a restart.

A rebuilt ``DefaultControlPlane`` / ``BudgetTracker`` starts with no clock,
so without this fix the first post-restart capacity check re-anchored the
duration window and handed the resumed run a *fresh* full
``max_duration_seconds`` — bypassing the cap entirely.

``BudgetTracker.rehydrate`` / ``DefaultControlPlane.rehydrate`` now accept
an optional ``elapsed_duration_seconds`` (the wall-clock time the original
run already spent). The tracker anchors its monotonic clock so that time is
never handed back, and — the safety fix — when a duration cap is configured
but no elapsed time is supplied it *fails closed* (the whole duration is
treated as spent) instead of resetting the window. Engagements with no
duration cap are unaffected.

Targeted unit scope only -- no graph/E2E execution here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.control.budget import BudgetTracker
from redteam.control.service import DefaultControlPlane
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
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId

ENGAGEMENT_ID = EngagementId("eng_duration")
RUN_ID = RunId("run_duration")


def _engagement(
    *,
    max_actions: int = 5,
    max_duration: int | None = None,
    per_minute: int = 30,
) -> Engagement:
    return Engagement(
        engagement_id=ENGAGEMENT_ID,
        name="duration",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(
            max_actions=max_actions, max_duration_seconds=max_duration
        ),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=per_minute),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(action_id: str = "action_1") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RUN_ID,
        engagement_id=ENGAGEMENT_ID,
        agent_id=AgentId("agent_duration"),
        capability="recon",
        operation="port_scan",
        target=HostTarget(host="good.local"),
        reason="duration resume test",
        created_at=datetime.now(timezone.utc),
    )


async def _evaluate(plane: DefaultControlPlane, action_id: str = "action_1"):
    return await plane.evaluate(
        _proposal(action_id), ControlContext(engagement_id=ENGAGEMENT_ID)
    )


# --- BudgetTracker: duration restore with an injected clock ------------


def test_rehydrate_restores_partial_elapsed_duration() -> None:
    tracker = BudgetTracker(
        ExecutionBudget(max_actions=100, max_duration_seconds=10)
    )
    tracker.rehydrate(actions_consumed=1, elapsed_duration_seconds=4.0, now=100.0)

    # 4s spent before restart + 5s since the anchor = 9s of the 10s window.
    assert tracker.has_capacity(now=105.0) is True
    assert tracker.has_capacity(now=106.0) is False
    assert tracker.consumed == 1
    assert tracker.remaining == 99


def test_rehydrate_without_elapsed_fails_closed_on_duration_cap() -> None:
    tracker = BudgetTracker(
        ExecutionBudget(max_actions=100, max_duration_seconds=10)
    )
    # Counters are still restored, but the unknown elapsed time is treated
    # as fully spent: no fresh window is handed out.
    tracker.rehydrate(actions_consumed=2, now=100.0)

    assert tracker.consumed == 2
    assert tracker.remaining == 98
    assert tracker.has_capacity(now=100.0) is False
    assert tracker.has_capacity(now=105.0) is False


def test_rehydrate_clamps_huge_elapsed_and_never_widens() -> None:
    tracker = BudgetTracker(
        ExecutionBudget(max_actions=100, max_duration_seconds=10)
    )
    tracker.rehydrate(actions_consumed=0, elapsed_duration_seconds=1e12, now=0.0)
    assert tracker.has_capacity(now=0.0) is False

    at_cap = BudgetTracker(
        ExecutionBudget(max_actions=100, max_duration_seconds=10)
    )
    at_cap.rehydrate(actions_consumed=0, elapsed_duration_seconds=10.0, now=0.0)
    assert at_cap.has_capacity(now=0.0) is False


def test_rehydrate_anchor_is_monotonic() -> None:
    tracker = BudgetTracker(
        ExecutionBudget(max_actions=100, max_duration_seconds=10)
    )
    tracker.rehydrate(actions_consumed=0, elapsed_duration_seconds=4.0, now=100.0)

    # A smaller elapsed must not move the anchor later (which would hand
    # back time); the earlier, more conservative anchor wins.
    tracker.rehydrate(actions_consumed=0, elapsed_duration_seconds=1.0, now=100.0)
    assert tracker.has_capacity(now=105.0) is True
    assert tracker.has_capacity(now=106.0) is False

    # A larger elapsed may move the anchor earlier (counts more time).
    tracker.rehydrate(actions_consumed=0, elapsed_duration_seconds=8.0, now=100.0)
    assert tracker.has_capacity(now=101.0) is True
    assert tracker.has_capacity(now=102.0) is False


def test_rehydrate_rejects_non_finite_and_negative_elapsed() -> None:
    tracker = BudgetTracker(
        ExecutionBudget(max_actions=100, max_duration_seconds=10)
    )
    for bad in (-1.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            tracker.rehydrate(
                actions_consumed=0, elapsed_duration_seconds=bad, now=0.0
            )


def test_rehydrate_without_duration_cap_ignores_elapsed() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_actions=5))
    tracker.rehydrate(
        actions_consumed=3, elapsed_duration_seconds=12_345.0, now=0.0
    )
    assert tracker.consumed == 3
    assert tracker.has_capacity(now=1e9) is True
    assert tracker.remaining == 2

    # No cap -> no re-anchoring, so an over-cap-free elapsed is inert.
    tracker.rehydrate(actions_consumed=3)
    assert tracker.has_capacity() is True

    # Invalid input is still rejected even without a cap.
    with pytest.raises(ValueError):
        tracker.rehydrate(actions_consumed=0, elapsed_duration_seconds=-1.0)


# --- DefaultControlPlane: restart duration accounting ------------------


async def test_plane_rehydrate_restores_elapsed_duration() -> None:
    engagement = _engagement(max_actions=10, max_duration=100)
    rebuilt = DefaultControlPlane({engagement.engagement_id: engagement})

    await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=1, elapsed_duration_seconds=1.0
    )
    # Well within the window: the resumed action is admitted.
    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_plane_rehydrate_at_duration_cap_fails_closed() -> None:
    engagement = _engagement(max_actions=10, max_duration=10)
    rebuilt = DefaultControlPlane({engagement.engagement_id: engagement})

    await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=1, elapsed_duration_seconds=10.0
    )
    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules


async def test_plane_rehydrate_no_elapsed_with_cap_fails_closed() -> None:
    """The defect: a restart used to reset the full duration window."""
    engagement = _engagement(max_actions=10, max_duration=10)
    rebuilt = DefaultControlPlane({engagement.engagement_id: engagement})

    await rebuilt.rehydrate(engagement.engagement_id, actions_consumed=1)
    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules


async def test_plane_rehydrate_huge_elapsed_is_clamped() -> None:
    engagement = _engagement(max_actions=10, max_duration=10)
    rebuilt = DefaultControlPlane({engagement.engagement_id: engagement})

    await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=0, elapsed_duration_seconds=1e9
    )
    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules


async def test_plane_rehydrate_without_duration_cap_is_unchanged() -> None:
    engagement = _engagement(max_actions=5)  # no duration cap
    rebuilt = DefaultControlPlane({engagement.engagement_id: engagement})

    restored = await rebuilt.rehydrate(
        engagement.engagement_id, actions_consumed=2, elapsed_duration_seconds=1e9
    )
    assert restored.budget_remaining == 3
    decision = await _evaluate(rebuilt)
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_plane_rehydrate_validates_elapsed() -> None:
    engagement = _engagement(max_duration=10)
    rebuilt = DefaultControlPlane({engagement.engagement_id: engagement})

    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            await rebuilt.rehydrate(
                engagement.engagement_id,
                actions_consumed=0,
                elapsed_duration_seconds=bad,
            )
