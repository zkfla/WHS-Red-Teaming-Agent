"""Denial severity tests (control-team, track-a).

Unit round-trip of the ``matched_rules`` tag convention, plus
end-to-end checks that ``DefaultControlPlane`` tags each denial with the
severity the orchestrator should react to.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.service import DefaultControlPlane
from redteam.control.severity import Severity, severity_from_matched_rules, to_tag
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId

# --- unit: tag round-trip ---------------------------------------------


def test_tag_round_trip() -> None:
    for sev in Severity:
        assert severity_from_matched_rules(["scope", to_tag(sev)]) is sev


def test_untagged_matched_rules_returns_none() -> None:
    assert severity_from_matched_rules(["scope", "roe"]) is None
    assert severity_from_matched_rules([]) is None


def test_malformed_tag_returns_none() -> None:
    assert severity_from_matched_rules(["severity:not_a_level"]) is None


# --- pipeline: DefaultControlPlane tags denials ------------------------


def _engagement(
    *,
    roe: list[RuleOfEngagement] | None = None,
    max_actions: int = 5,
) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_sev"),
        name="sev",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        rules_of_engagement=roe or [],
        execution_budget=ExecutionBudget(max_actions=max_actions),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(
    *, capability: str = "recon", operation: str = "port_scan", host: str = "good.local"
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_sev"),
        run_id=RunId("run_sev"),
        engagement_id=EngagementId("eng_sev"),
        agent_id=AgentId("agent_sev"),
        capability=capability,
        operation=operation,
        target=HostTarget(host=host),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


async def _evaluate(engagement: Engagement, proposal: ActionProposal):
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    return await plane.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )


async def test_scope_denial_tagged_block_action() -> None:
    decision = await _evaluate(_engagement(), _proposal(host="evil.example"))
    assert decision.verdict == PolicyVerdict.DENY
    assert severity_from_matched_rules(decision.matched_rules) is Severity.BLOCK_ACTION


async def test_roe_capability_denial_tagged_replan_goal() -> None:
    roe = [RuleOfEngagement(name="no-mobile", denied_capabilities=["recon"])]
    decision = await _evaluate(_engagement(roe=roe), _proposal())
    assert decision.verdict == PolicyVerdict.DENY
    assert severity_from_matched_rules(decision.matched_rules) is Severity.REPLAN_GOAL


async def test_roe_time_window_denial_tagged_abort_run() -> None:
    # start_at far in the future -> "window has not started" -> ABORT_RUN.
    # Uses a fixed future date so it never depends on the wall clock.
    roe = [RuleOfEngagement(name="future", start_at=datetime(2100, 1, 1, tzinfo=timezone.utc))]
    decision = await _evaluate(_engagement(roe=roe), _proposal())
    assert decision.verdict == PolicyVerdict.DENY
    assert severity_from_matched_rules(decision.matched_rules) is Severity.ABORT_RUN


async def test_budget_exhaustion_tagged_abort_run() -> None:
    decision = await _evaluate(_engagement(max_actions=0), _proposal())
    assert decision.verdict == PolicyVerdict.DENY
    assert "budget" in decision.matched_rules
    assert severity_from_matched_rules(decision.matched_rules) is Severity.ABORT_RUN


async def test_allow_has_no_severity_tag() -> None:
    decision = await _evaluate(_engagement(), _proposal())
    assert decision.verdict == PolicyVerdict.ALLOW
    assert severity_from_matched_rules(decision.matched_rules) is None
