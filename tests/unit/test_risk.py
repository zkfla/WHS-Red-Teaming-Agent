"""Risk-tiered approval tests (control-team, track-a).

Covers: risk ladder comparison, RegistryRiskResolver (reads the Tool
Registry via list_tools_for_action, takes the highest risk), the policy
engine's risk-tiered approval, and the full DefaultControlPlane pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.policy import evaluate_policy
from redteam.control.risk import (
    DEFAULT_APPROVAL_RISK_THRESHOLD,
    RegistryRiskResolver,
    risk_meets_threshold,
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
from redteam.core.contracts.tool import ToolDescriptor, ToolInvocation, ToolResult
from redteam.core.enums import PolicyVerdict, RiskLevel
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId, ToolId


def _proposal(*, capability: str = "recon", operation: str = "port_scan") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_risk"),
        run_id=RunId("run_risk"),
        engagement_id=EngagementId("eng_risk"),
        agent_id=AgentId("agent_risk"),
        capability=capability,
        operation=operation,
        target=HostTarget(host="good.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def _tool(risk: RiskLevel) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("recon.example"),
        server_id="srv",
        tool_name="example",
        capability_tags=["recon"],
        risk_level=risk,
    )


class _FakeGateway:
    """Minimal ``ToolGateway`` double — only ``list_tools_for_action`` is
    exercised by these tests, but ``invoke`` must exist (even unused) to
    structurally satisfy the Protocol under mypy."""

    def __init__(self, tools: list[ToolDescriptor]) -> None:
        self._tools = tools

    def list_tools_for_action(self, capability: str, operation: str) -> list[ToolDescriptor]:
        return self._tools

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        raise NotImplementedError("not exercised by risk resolver tests")


class _FixedRiskResolver:
    def __init__(self, level: RiskLevel | None) -> None:
        self._level = level

    def resolve(self, capability: str, operation: str) -> RiskLevel | None:
        return self._level


# --- unit: ladder + resolver ------------------------------------------


def test_risk_meets_threshold() -> None:
    assert risk_meets_threshold(RiskLevel.HIGH, RiskLevel.HIGH) is True
    assert risk_meets_threshold(RiskLevel.CRITICAL, RiskLevel.HIGH) is True
    assert risk_meets_threshold(RiskLevel.MEDIUM, RiskLevel.HIGH) is False
    assert risk_meets_threshold(RiskLevel.LOW, RiskLevel.LOW) is True


def test_registry_resolver_takes_highest_risk() -> None:
    gw = _FakeGateway([_tool(RiskLevel.LOW), _tool(RiskLevel.HIGH), _tool(RiskLevel.MEDIUM)])
    resolver = RegistryRiskResolver(gw)
    assert resolver.resolve("recon", "port_scan") is RiskLevel.HIGH


def test_registry_resolver_none_when_no_tools() -> None:
    resolver = RegistryRiskResolver(_FakeGateway([]))
    assert resolver.resolve("recon", "port_scan") is None


# --- unit: policy risk tier -------------------------------------------


def test_policy_requires_approval_when_risk_at_threshold() -> None:
    verdict, reasons, matched = evaluate_policy(
        _proposal(), ApprovalPolicy(), risk=RiskLevel.HIGH, risk_threshold=RiskLevel.HIGH
    )
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert matched == ["risk:high"]
    assert "risk_level 'high'" in reasons[0]


def test_policy_allows_when_risk_below_threshold() -> None:
    verdict, _, _ = evaluate_policy(
        _proposal(), ApprovalPolicy(), risk=RiskLevel.MEDIUM, risk_threshold=RiskLevel.HIGH
    )
    assert verdict == PolicyVerdict.ALLOW


def test_policy_allows_when_no_risk() -> None:
    verdict, _, _ = evaluate_policy(_proposal(), ApprovalPolicy())
    assert verdict == PolicyVerdict.ALLOW


# --- pipeline: DefaultControlPlane ------------------------------------


def _engagement() -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_risk"),
        name="risk",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),  # nothing listed -> only risk drives approval
        created_at=datetime.now(timezone.utc),
    )


async def _evaluate(resolver: _FixedRiskResolver):
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement}, risk_resolver=resolver)
    return await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )


async def test_pipeline_high_risk_requires_approval() -> None:
    decision = await _evaluate(_FixedRiskResolver(RiskLevel.HIGH))
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


async def test_pipeline_low_risk_allows() -> None:
    decision = await _evaluate(_FixedRiskResolver(RiskLevel.LOW))
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_pipeline_no_resolver_allows() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})  # no resolver
    decision = await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.ALLOW


class _ExplodingResolver:
    def resolve(self, capability: str, operation: str) -> RiskLevel | None:
        raise RuntimeError("registry unavailable")


async def test_pipeline_risk_resolution_failure_escalates_to_manual_approval() -> None:
    # A crashing risk source must NOT crash the gate, and must escalate to
    # a human (REQUIRE_APPROVAL, not a fail-closed DENY) rather than let a
    # possibly-risky action through on an unresolved risk.
    engagement = _engagement()
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement}, risk_resolver=_ExplodingResolver()
    )
    decision = await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert "risk:unresolved" in decision.matched_rules
    assert decision.approval_id is not None


def test_default_threshold_is_high() -> None:
    assert DEFAULT_APPROVAL_RISK_THRESHOLD is RiskLevel.HIGH
