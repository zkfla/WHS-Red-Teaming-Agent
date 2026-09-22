"""Minimal reconciliation host loop: drives reconcile_once() repeatedly."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from redteam.attack_graph.service import AttackGraphService
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ApprovalStatus, GraphNodeType, ToolHealthStatus
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.orchestration.agents import MOBILE_AGENT, AgentRegistry
from redteam.orchestration.reconciliation import run_reconciliation_loop
from redteam.orchestration.runner import RECONCILABLE_REASONS, AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from tests.fakes import DummyToolGateway, build_test_application


def test_approval_pending_is_a_reconcilable_reason() -> None:
    assert TerminationReason.APPROVAL_PENDING in RECONCILABLE_REASONS


async def test_host_loop_stops_immediately_when_stop_is_already_set() -> None:
    engagement = Engagement(
        engagement_id=EngagementId("eng_host"),
        name="host",
        scope=EngagementScope(),
        execution_budget=ExecutionBudget(max_actions=1),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=1),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=True))
    runner = AgentLoopRunner(deps, settings=deps.settings)
    runtime = RuntimeState(
        run_id=RunId("run_host"),
        engagement_id=engagement.engagement_id,
        termination_reason=TerminationReason.NO_ACTIONABLE_PLAN,
    )
    stop = asyncio.Event()
    stop.set()

    final = await run_reconciliation_loop(runner, runtime, stop=stop, poll_interval_seconds=5)

    assert final is runtime  # never resumed -- stop was already set


async def test_host_loop_resumes_an_approval_pending_run_once_resolved() -> None:
    engagement = Engagement(
        engagement_id=EngagementId("eng_host_appr"),
        name="host-approval",
        scope=EngagementScope(allowed_assets=["evil.apk"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(require_approval_for=["mobile"]),
        created_at=datetime.now(timezone.utc),
    )
    control_plane = DefaultControlPlane({engagement.engagement_id: engagement})
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("mobile.artifact_analyze"),
            server_id="test",
            tool_name="artifact_analyze",
            capability_tags=["mobile"],
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    catalog = MCPToolGateway(registry, {}, {"mobile.artifact_analyze": ["static_analysis"]})
    gateway = DummyToolGateway()
    deps = build_test_application(
        control_plane=control_plane,
        tool_gateway=gateway,
        tool_catalog=catalog,
        agents=AgentRegistry([MOBILE_AGENT]),
        settings=OrchestrationSettings(
            checkpointing=True, max_steps=10, checkpoint_backend="memory"
        ),
    )
    await AttackGraphService(deps.graph_store).upsert_node(GraphNodeType.ASSET, key="evil.apk")
    runner = AgentLoopRunner(deps, settings=deps.settings)
    initial = RuntimeState(
        run_id=RunId("run_host_appr"),
        engagement_id=engagement.engagement_id,
        remaining_budget=engagement.execution_budget.max_actions,
    )

    stopped = await runner.run(initial)
    assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
    assert stopped.pending_approval_id is not None

    # Nobody has resolved it yet -- one tick must not resume anything, and
    # the loop must not busy-poll (single short tick here, in a real host
    # this would be a much longer poll_interval_seconds).
    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.03)
        stop.set()

    asyncio.create_task(_stop_soon())
    still_pending = await run_reconciliation_loop(
        runner, stopped, stop=stop, poll_interval_seconds=0.05
    )
    assert still_pending.termination_reason == TerminationReason.APPROVAL_PENDING
    assert gateway.invocations == []

    # Now resolve it, and let the host loop pick that up on its own. Give
    # it a stop deadline: once resumed, a single-step playbook agent
    # reaches "no_actionable_plan" next, which is itself reconcilable
    # (a later graph change could revive it) -- a real host keeps polling
    # that indefinitely, this test just needs to observe the one resume.
    resume_stop = asyncio.Event()

    async def _stop_after_one_more_tick() -> None:
        await asyncio.sleep(0.15)
        resume_stop.set()

    asyncio.create_task(_stop_after_one_more_tick())
    await control_plane.resolve_approval(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )
    final = await run_reconciliation_loop(
        runner, still_pending, stop=resume_stop, poll_interval_seconds=0.05
    )

    assert final.actions_executed == 1
    assert final.termination_reason != TerminationReason.APPROVAL_PENDING
    assert [str(inv.tool_id) for inv in gateway.invocations] == ["mobile.artifact_analyze"]
