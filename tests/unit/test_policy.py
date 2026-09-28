"""Policy engine tests (control-team, track-a).

Approval gating matches either a capability namespace or a canonical
action key ``<capability>.<operation>`` — consistent with ROE and
docs/contracts/actions.md. Denials are ROE's job, not the policy engine's.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.policy import (
    auto_approval_ceiling,
    evaluate_policy,
    may_auto_approve,
    resolve_risk_threshold,
)
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
from redteam.core.enums import PolicyVerdict, RiskLevel
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


def _proposal(*, capability: str = "recon", operation: str = "port_scan") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_pol"),
        run_id=RunId("run_pol"),
        engagement_id=EngagementId("eng_pol"),
        agent_id=AgentId("agent_pol"),
        capability=capability,
        operation=operation,
        target=HostTarget(host="good.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


# --- unit: evaluate_policy ---------------------------------------------


def test_no_rule_allows() -> None:
    verdict, _, matched = evaluate_policy(_proposal(), ApprovalPolicy())
    assert verdict == PolicyVerdict.ALLOW
    assert matched == []


def test_capability_level_approval() -> None:
    policy = ApprovalPolicy(require_approval_for=["mobile"])
    verdict, _, matched = evaluate_policy(_proposal(capability="mobile"), policy)
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert matched == ["mobile"]


def test_action_key_level_approval() -> None:
    policy = ApprovalPolicy(require_approval_for=["recon.port_scan"])
    verdict, _, matched = evaluate_policy(_proposal(), policy)
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert matched == ["recon.port_scan"]


def test_bare_operation_does_not_match() -> None:
    # Must be the canonical key "recon.port_scan", not bare "port_scan".
    policy = ApprovalPolicy(require_approval_for=["port_scan"])
    verdict, _, _ = evaluate_policy(_proposal(), policy)
    assert verdict == PolicyVerdict.ALLOW


# --- pipeline: approval path through DefaultControlPlane ---------------


def _engagement(approval: ApprovalPolicy) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_pol"),
        name="pol",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=approval,
        created_at=datetime.now(timezone.utc),
    )


async def _evaluate(engagement: Engagement):
    plane = DefaultControlPlane({engagement.engagement_id: engagement})
    return await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )


async def test_pipeline_requires_approval_and_creates_approval_id() -> None:
    approval = ApprovalPolicy(require_approval_for=["recon.port_scan"], auto_approve=False)
    decision = await _evaluate(_engagement(approval))
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


async def test_legacy_auto_approve_does_not_bypass_unknown_risk() -> None:
    approval = ApprovalPolicy(require_approval_for=["recon.port_scan"], auto_approve=True)
    decision = await _evaluate(_engagement(approval))
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


# --- unit: configurable threshold + bounded auto-approval helpers -------


def test_resolve_risk_threshold_prefers_policy_value() -> None:
    assert resolve_risk_threshold(ApprovalPolicy()) is RiskLevel.HIGH
    assert resolve_risk_threshold(ApprovalPolicy(risk_threshold=RiskLevel.LOW)) is RiskLevel.LOW


def test_policy_threshold_applies_without_explicit_argument() -> None:
    # The engagement's own threshold drives risk-tiered approval when the
    # caller does not pass one explicitly.
    policy = ApprovalPolicy(risk_threshold=RiskLevel.LOW)
    verdict, _, matched = evaluate_policy(_proposal(), policy, risk=RiskLevel.LOW)
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert matched == ["risk:low"]


def test_explicit_argument_overrides_policy_threshold() -> None:
    policy = ApprovalPolicy(risk_threshold=RiskLevel.CRITICAL)
    verdict, _, _ = evaluate_policy(
        _proposal(), policy, risk=RiskLevel.MEDIUM, risk_threshold=RiskLevel.LOW
    )
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL


def test_auto_approval_ceiling_precedence_and_legacy_mapping() -> None:
    assert auto_approval_ceiling(ApprovalPolicy()) is None
    assert auto_approval_ceiling(ApprovalPolicy(auto_approve=True)) is RiskLevel.LOW
    # An explicit bounded ceiling wins over the legacy flag.
    assert (
        auto_approval_ceiling(ApprovalPolicy(auto_approve=True, auto_approve_up_to=RiskLevel.LOW))
        is RiskLevel.LOW
    )


def test_may_auto_approve_respects_ceiling_and_unknown_risk() -> None:
    bounded = ApprovalPolicy(auto_approve_up_to=RiskLevel.HIGH)
    assert may_auto_approve(bounded, RiskLevel.HIGH) is True
    assert may_auto_approve(bounded, RiskLevel.CRITICAL) is False
    # Unknown risk may exceed the ceiling -> never auto-approved.
    assert may_auto_approve(bounded, None) is False
    # Legacy config is accepted, but cannot approve unknown or high risk.
    legacy = ApprovalPolicy(auto_approve=True)
    assert may_auto_approve(legacy, None) is False
    assert may_auto_approve(legacy, RiskLevel.HIGH) is False
    assert may_auto_approve(legacy, RiskLevel.LOW) is True
    assert may_auto_approve(ApprovalPolicy(), RiskLevel.LOW) is False


class _LevelResolver:
    def __init__(self, level: RiskLevel) -> None:
        self._level = level

    def resolve(self, capability: str, operation: str) -> RiskLevel:
        return self._level


async def _evaluate_with_risk(approval: ApprovalPolicy, risk: RiskLevel):
    engagement = _engagement(approval)
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement}, risk_resolver=_LevelResolver(risk)
    )
    return await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )


async def test_bounded_auto_approve_allows_risk_at_or_below_ceiling() -> None:
    approval = ApprovalPolicy(
        require_approval_for=["recon.port_scan"],
        auto_approve_up_to=RiskLevel.HIGH,
    )
    decision = await _evaluate_with_risk(approval, RiskLevel.MEDIUM)
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_bounded_auto_approve_requires_approval_above_ceiling() -> None:
    approval = ApprovalPolicy(
        require_approval_for=["recon.port_scan"],
        auto_approve_up_to=RiskLevel.HIGH,
    )
    decision = await _evaluate_with_risk(approval, RiskLevel.CRITICAL)
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


async def test_bounded_auto_approve_never_approves_unknown_risk() -> None:
    # A bounded ceiling cannot prove an unresolved risk is below it, so a
    # human is still required, including under legacy auto_approve.
    approval = ApprovalPolicy(
        require_approval_for=["recon.port_scan"],
        auto_approve_up_to=RiskLevel.CRITICAL,
    )
    decision = await _evaluate(_engagement(approval))  # no resolver -> risk None
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
