"""Control Plane universality (control-team, track-a) — PM problem 2.

Complements the two existing no-bypass guarantees:
  - ``test_control_plane_bypass`` — Orchestration has no import path to
    Control/Capability internals (structural).
  - ``test_conversion`` — a non-ALLOW ``PolicyDecision`` can never become
    a ``ToolInvocation`` (mechanical, ``build_tool_invocation``).

This test adds the behavioral guarantee: **every** action type funnels
through ``ControlPlane.evaluate`` and comes back with a real
``PolicyDecision`` — including new action families (auth attempt,
scanner, replay, active probe, mobile) and a never-before-seen
capability/operation. The gate is action-type-agnostic; there is no
per-type code path that could skip it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.target import AssetTarget, HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, RunId

_ENG = EngagementId("eng_univ")
_HOST = HostTarget(host="good.local")
_ASSET = AssetTarget(asset_id=AssetId("com.example.app"))

# Representative action families a growing Agent may emit (PM problem 2),
# plus a deliberately novel (capability, operation, target) the gate has
# never been configured for.
_ACTION_TYPES = [
    ("recon", "port_scan", _HOST),
    ("recon", "service_discovery", _HOST),  # active probe
    ("web", "auth_attempt", _HOST),
    ("web", "http_request", _HOST),  # replay-style
    ("web", "parameter_fuzz", _HOST),  # scanner-style
    ("mobile", "app_launch", _ASSET),
    ("analysis", "static_analysis", _HOST),
    ("future_capability", "never_seen_operation", _HOST),  # novel type
]


def _engagement() -> Engagement:
    return Engagement(
        engagement_id=_ENG,
        name="universality",
        scope=EngagementScope(allowed_hosts=["good.local"], allowed_assets=["com.example.app"]),
        # Generous limits so budget/rate never short-circuit — we want to
        # prove the gate *evaluates* every type, not that it allows them.
        execution_budget=ExecutionBudget(max_actions=1000),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=1000),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(index: int, capability: str, operation: str, target) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(f"action_{index}"),
        run_id=RunId("run_univ"),
        engagement_id=_ENG,
        agent_id=AgentId("agent_univ"),
        capability=capability,
        operation=operation,
        target=target,
        reason="universality test",
        created_at=datetime.now(timezone.utc),
    )


async def test_every_action_type_goes_through_the_gate() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    context = ControlContext(engagement_id=_ENG)

    for index, (capability, operation, target) in enumerate(_ACTION_TYPES):
        proposal = _proposal(index, capability, operation, target)
        decision = await plane.evaluate(proposal, context)

        # The gate returned a real, correlated decision for this type —
        # it did not skip, bypass, or crash on any of them.
        assert isinstance(decision, PolicyDecision), f"{capability}.{operation} skipped the gate"
        assert decision.action_id == proposal.action_id
        assert decision.verdict in {
            PolicyVerdict.ALLOW,
            PolicyVerdict.DENY,
            PolicyVerdict.REQUIRE_APPROVAL,
        }


async def test_novel_action_type_is_still_evaluated_not_skipped() -> None:
    """A capability/operation the framework has never seen must still be
    judged (here: allowed, since it is in scope and unrestricted) rather
    than fall through some default-allow bypass."""
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    decision = await plane.evaluate(
        _proposal(99, "future_capability", "never_seen_operation", _HOST),
        ControlContext(engagement_id=_ENG),
    )
    assert decision.verdict == PolicyVerdict.ALLOW
    assert decision.action_id == ActionId("action_99")
