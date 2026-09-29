"""End-to-end Approval resume: REQUIRE_APPROVAL -> resolve -> same Run continues.

```text
ActionProposal -> Control -> REQUIRE_APPROVAL -> run stops (approval_pending)
                                   |
                          human resolves Approval
                                   |
        AgentLoopRunner.reconcile_once() resumes the SAME run at POLICY
                                   |
                     approval_outcome() -> ALLOW/DENY
                                   |
                    EXECUTE (only on ALLOW) -> VERIFY -> UPDATE -> REPLAN
```

Uses a real ``DefaultControlPlane`` (not ``FakeControlPlane``): the whole
point is that resume goes back through Control, never around it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.patterns import NodePattern
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
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId
from redteam.orchestration.agents import AgentRegistry, AgentSpec, PlanStep
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import DummyToolGateway, build_test_application

MOBILE_APPROVAL_AGENT = AgentSpec(
    agent_id=AgentId("mobile-approval-test"),
    name="Mobile approval test agent",
    capabilities=["mobile"],
    playbook=[
        PlanStep(
            step_id="launch-app",
            capability="mobile",
            operation="app_launch",
            tool_id=ToolId("mobile.mobile_launch_app"),
            target_bind="asset",
            preconditions=[NodePattern(type=GraphNodeType.ASSET, bind="asset")],
            reason="Exercise the approval and resume path.",
        )
    ],
)


def _engagement(*, max_actions: int = 5) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_mobile"),
        name="mobile approval",
        scope=EngagementScope(allowed_assets=["evil.apk"], allowed_hosts=["anything"]),
        execution_budget=ExecutionBudget(max_actions=max_actions),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(require_approval_for=["mobile"]),
        created_at=datetime.now(timezone.utc),
    )


def _mobile_catalog() -> MCPToolGateway:
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("mobile.mobile_launch_app"),
            server_id="test",
            tool_name="mobile_launch_app",
            capability_tags=["mobile"],
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    return MCPToolGateway(
        registry, {}, {"mobile.mobile_launch_app": ["app_launch"]}
    )


async def _setup(*, max_actions: int = 5):
    engagement = _engagement(max_actions=max_actions)
    control_plane = DefaultControlPlane({engagement.engagement_id: engagement})
    gateway = DummyToolGateway()
    deps = build_test_application(
        control_plane=control_plane,
        tool_gateway=gateway,
        tool_catalog=_mobile_catalog(),
        agents=AgentRegistry([MOBILE_APPROVAL_AGENT]),
        settings=OrchestrationSettings(
            checkpointing=True, max_steps=10, checkpoint_backend="memory"
        ),
    )
    await AttackGraphService(deps.graph_store).upsert_node(GraphNodeType.ASSET, key="evil.apk")
    runner = AgentLoopRunner(deps, settings=deps.settings)
    initial = RuntimeState(
        run_id=RunId("run_mobile"),
        engagement_id=engagement.engagement_id,
        remaining_budget=engagement.execution_budget.max_actions,
    )
    return engagement, control_plane, gateway, deps, runner, initial


async def test_require_approval_then_approve_then_allow_and_execute() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup()

    stopped = await runner.run(initial)
    assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
    assert stopped.pending_approval_id is not None
    assert stopped.actions_executed == 0
    assert gateway.invocations == []

    await control_plane.resolve_approval(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )
    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True
    assert result.runtime.actions_executed == 1
    assert result.runtime.run_id == initial.run_id  # same Run, not a new one
    assert result.runtime.pending_approval_id is None  # consumed, not left dangling
    assert [str(inv.tool_id) for inv in gateway.invocations] == ["mobile.mobile_launch_app"]
    # The original WorkItem (leased before REQUIRE_APPROVAL) was already
    # finalized BLOCKED by UPDATE at that point -- the resumed execution is
    # a deliberate one-off outside the Work Ledger's lease tracking, not a
    # re-lease of that same row. See runner._resume_pending_approval.
    blocked = await deps.work_store.list_items(run_id=initial.run_id, status=WorkStatus.BLOCKED)
    assert len(blocked) == 1


async def test_require_approval_then_reject_then_never_executes() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup()

    stopped = await runner.run(initial)
    assert stopped.pending_approval_id is not None

    await control_plane.resolve_approval(
        stopped.pending_approval_id, ApprovalStatus.REJECTED, "operator"
    )
    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True
    assert result.runtime.actions_executed == 0
    assert gateway.invocations == []
    work = await deps.work_store.list_items(run_id=initial.run_id, status=WorkStatus.BLOCKED)
    assert len(work) == 1


async def test_duplicate_resolution_is_rejected() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup()
    stopped = await runner.run(initial)
    approval_id = stopped.pending_approval_id
    assert approval_id is not None
    await control_plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")

    import pytest

    with pytest.raises(ValueError, match="already"):
        await control_plane.resolve_approval(approval_id, ApprovalStatus.REJECTED, "operator_two")


async def test_stale_approval_never_resumes_a_run_it_cannot_safely_identify() -> None:
    """A resolved Approval whose ControlPlane instance never held (or
    already consumed) the original proposal must not be treated as
    resumable -- reconcile_once() must leave the run exactly as
    terminated rather than guessing."""
    engagement, control_plane, gateway, deps, runner, initial = await _setup()
    stopped = await runner.run(initial)
    approval_id = stopped.pending_approval_id
    assert approval_id is not None

    await control_plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")
    # Consume the outcome once already (simulates a prior successful
    # resume, or a fresh ControlPlane after a process restart sharing the
    # same durable ApprovalStore but none of the in-memory proposal state).
    control_plane._pending_proposals.pop(approval_id, None)  # noqa: SLF001
    control_plane._resolved_outcomes.pop(approval_id, None)  # noqa: SLF001

    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True  # approval_outcome() still resolves...
    assert result.runtime.actions_executed == 0  # ...but as a fail-closed DENY, not a guess
    assert gateway.invocations == []


async def test_reevaluation_denies_when_budget_exhausted_meanwhile() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup(max_actions=1)
    stopped = await runner.run(initial)
    approval_id = stopped.pending_approval_id
    assert approval_id is not None

    # Consume the engagement's only unit of budget via a separate,
    # immediately-ALLOWed action while this one waits on a human.
    from redteam.core.contracts.action import ActionProposal
    from redteam.core.contracts.policy import ControlContext
    from redteam.core.contracts.target import HostTarget
    from redteam.core.enums import PolicyVerdict
    from redteam.core.ids import ActionId, AgentId

    other = ActionProposal(
        action_id=ActionId("action_other"),
        run_id=initial.run_id,
        engagement_id=engagement.engagement_id,
        agent_id=AgentId("agent_1"),
        capability="recon",
        operation="port_scan",
        target=HostTarget(host="anything"),
        reason="consume budget",
        created_at=datetime.now(timezone.utc),
    )
    other_decision = await control_plane.evaluate(
        other, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert other_decision.verdict == PolicyVerdict.ALLOW

    await control_plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")
    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True
    assert result.runtime.actions_executed == 0
    assert gateway.invocations == []
