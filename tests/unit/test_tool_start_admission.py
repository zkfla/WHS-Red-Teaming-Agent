"""Server queue waiting must precede fresh, non-charging policy admission."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.control import service as control_module
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import PolicyVerdict, ToolHealthStatus
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId, ToolId
from redteam.core.ports.tool_gateway import ToolStartRejected


class BlockingAdapter:
    server_id = "queue"

    def __init__(self, tool: ToolDescriptor) -> None:
        self.tool = tool
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def discover_tools(self) -> list[ToolDescriptor]:
        return [self.tool]

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        self.calls.append(arguments["marker"])
        self.started.set()
        await self.release.wait()
        now = datetime.now(timezone.utc)
        return MCPCallResult(True, now, now, {"ok": True})


async def test_last_reserved_slot_is_rechecked_after_server_queue(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    current = [now]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    monkeypatch.setattr(control_module, "datetime", Clock)
    tool = ToolDescriptor(
        tool_id=ToolId("sandbox.queue.probe"),
        server_id="queue",
        tool_name="probe",
        capability_tags=["sandbox"],
        operations=["probe_check"],
        health_status=ToolHealthStatus.HEALTHY,
    )
    registry = MCPToolRegistry()
    registry.register(tool)
    adapter = BlockingAdapter(tool)
    gateway = MCPToolGateway(registry, {"queue": adapter}, health_refresh_enabled=False)
    engagement = Engagement(
        engagement_id=EngagementId("eng-queue"),
        name="queue admission",
        scope=EngagementScope(allowed_hosts=["a.local"]),
        rules_of_engagement=[RuleOfEngagement(name="window", end_at=now + timedelta(minutes=1))],
        execution_budget=ExecutionBudget(max_actions=1, max_tool_calls=1),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=now,
    )
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        risk_resolver=RegistryRiskResolver(gateway),
    )
    proposal = ActionProposal(
        action_id=ActionId("queued"),
        run_id=RunId("run-queue"),
        engagement_id=engagement.engagement_id,
        agent_id=AgentId("sandbox-agent"),
        capability="sandbox",
        operation="probe_check",
        target=HostTarget(host="a.local"),
        arguments={"marker": "queued"},
        reason="authorized probe",
        created_at=now,
    )
    context = ControlContext(engagement_id=engagement.engagement_id)
    decision = await control.evaluate(proposal, context)
    assert decision.verdict is PolicyVerdict.ALLOW
    assert decision.budget_remaining == 0
    invocation = build_tool_invocation(proposal, decision, tool_id=tool.tool_id)
    receipt = str(invocation.invocation_id)
    admitted = await control.admit_invocation(proposal, decision, context, receipt_id=receipt)
    assert admitted.verdict is PolicyVerdict.ALLOW

    # Occupy the physical server queue without changing this action's accounting.
    occupying = asyncio.create_task(
        gateway.invoke(
            invocation.model_copy(
                update={"arguments": {"marker": "occupier"}},
            )
        )
    )
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    fresh: list[PolicyDecision] = []

    async def before_start() -> None:
        checked = await control.admit_invocation(proposal, decision, context, receipt_id=receipt)
        fresh.append(checked)
        if checked.verdict is not PolicyVerdict.ALLOW:
            raise ToolStartRejected("window closed while queued")

    queued = asyncio.create_task(gateway.invoke_with_admission(invocation, before_start))
    await asyncio.sleep(0)
    current[0] = now + timedelta(minutes=2)
    adapter.release.set()
    assert (await occupying).success
    with pytest.raises(ToolStartRejected, match="window closed"):
        await asyncio.wait_for(queued, timeout=2)
    assert len(fresh) == 1 and fresh[0].verdict is PolicyVerdict.DENY
    assert "roe_window_closed" in fresh[0].reasons
    assert adapter.calls == ["occupier"]
