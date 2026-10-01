"""Durable resume: recover a checkpoint in a fresh process and resume safely.

Prompt 6 split from the in-process ``reconcile_once`` path:

    process 1: run -> APPROVAL_PENDING (durable sqlite checkpoint + Approval)
    process 2: recover_runtime(run_id) -> resume_recovered(runtime)

Only an explicitly re-linked checkpointed proposal may resume; a checkpoint
whose next super-step is EXECUTE/WORKER is never replayed; and a resolved
denial goes back through the REPLAN path rather than executing.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import AgentPhase, ApprovalStatus, GraphNodeType, ToolHealthStatus
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.orchestration.agents import MOBILE_AGENT, AgentRegistry
from redteam.orchestration.runner import AgentLoopRunner, CheckpointingDisabledError
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from tests.fakes import DummyToolGateway, build_test_application

RUN_ID = "run_durable"


def _engagement(*, max_actions: int = 5) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_durable"),
        name="durable resume",
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
            tool_id=ToolId("mobile.artifact_analyze"),
            server_id="test",
            tool_name="artifact_analyze",
            capability_tags=["mobile"],
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    return MCPToolGateway(registry, {}, {"mobile.artifact_analyze": ["static_analysis"]})


def _durable_settings(tmp_path) -> OrchestrationSettings:  # noqa: ANN001
    return OrchestrationSettings(
        checkpointing=True,
        checkpoint_backend="sqlite",
        checkpoint_path=str(tmp_path / "checkpoints.sqlite3"),
        max_steps=10,
    )


def _plane(
    engagement: Engagement, approval_dir, audit_dir  # noqa: ANN001
) -> DefaultControlPlane:
    return DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=AuditLog(FilesystemAuditStore(audit_dir)),
        approvals=ApprovalService(FilesystemApprovalStore(approval_dir)),
    )


async def _run_to_pending(tmp_path, *, max_actions: int = 5):  # noqa: ANN202
    engagement = _engagement(max_actions=max_actions)
    approval_dir = tmp_path / "approvals"
    audit_dir = tmp_path / "audit"
    settings = _durable_settings(tmp_path)
    plane = _plane(engagement, approval_dir, audit_dir)
    gateway = DummyToolGateway()
    deps = build_test_application(
        control_plane=plane,
        tool_gateway=gateway,
        tool_catalog=_mobile_catalog(),
        agents=AgentRegistry([MOBILE_AGENT]),
        settings=settings,
    )
    await AttackGraphService(deps.graph_store).upsert_node(GraphNodeType.ASSET, key="evil.apk")
    runner = AgentLoopRunner(deps, settings=settings)
    stopped = await runner.run(
        RuntimeState(
            run_id=RunId(RUN_ID),
            engagement_id=engagement.engagement_id,
            remaining_budget=engagement.execution_budget.max_actions,
        )
    )
    await runner.aclose()
    assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
    assert stopped.pending_approval_id is not None
    return engagement, settings, approval_dir, audit_dir, stopped


def _second_process(  # noqa: ANN202
    engagement: Engagement, settings: OrchestrationSettings, approval_dir, audit_dir  # noqa: ANN001
):
    plane = _plane(engagement, approval_dir, audit_dir)
    gateway = DummyToolGateway()
    deps = build_test_application(
        control_plane=plane,
        tool_gateway=gateway,
        tool_catalog=_mobile_catalog(),
        agents=AgentRegistry([MOBILE_AGENT]),
        settings=settings,
    )
    return AgentLoopRunner(deps, settings=settings), plane, gateway


async def test_recover_runtime_reads_a_durable_checkpoint_in_a_fresh_runner(tmp_path) -> None:
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)

    runner, _, _ = _second_process(engagement, settings, approval_dir, audit_dir)
    recovered = await runner.recover_runtime(RUN_ID)
    await runner.aclose()

    assert recovered is not None
    assert recovered.run_id == stopped.run_id
    assert recovered.termination_reason == TerminationReason.APPROVAL_PENDING
    assert recovered.pending_approval_id == stopped.pending_approval_id


async def test_raw_checkpoint_read_agrees_with_runner_recovery(tmp_path) -> None:
    """The CLI reads the engagement id directly from the checkpoint before
    building; it must match what the full runner later recovers."""
    from redteam.app.cli.commands import resume as resume_command

    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    hint = await resume_command._read_checkpoint_runtime(settings, RUN_ID)

    assert hint is not None
    assert hint.engagement_id == engagement.engagement_id
    assert hint.run_id == stopped.run_id
    assert hint.termination_reason == TerminationReason.APPROVAL_PENDING
    assert hint.pending_approval_id == stopped.pending_approval_id


async def test_approved_durable_resume_executes_the_original_action(tmp_path) -> None:
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    # Process 1 is gone; the human approves through the durable store.
    await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )

    runner, _, gateway = _second_process(engagement, settings, approval_dir, audit_dir)
    try:
        runtime = await runner.recover_runtime(RUN_ID)
        assert runtime is not None
        final = await runner.resume_recovered(runtime)
    finally:
        await runner.aclose()

    assert final.run_id == stopped.run_id
    assert final.phase is AgentPhase.FINISHED
    assert final.actions_executed == 1
    assert final.pending_approval_id is None
    assert [str(inv.tool_id) for inv in gateway.invocations] == ["mobile.artifact_analyze"]


async def test_two_concurrent_resumers_can_claim_one_approved_action_only(tmp_path) -> None:
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    first, _, first_gateway = _second_process(engagement, settings, approval_dir, audit_dir)
    second, _, second_gateway = _second_process(engagement, settings, approval_dir, audit_dir)
    try:
        first_state, second_state = await asyncio.gather(
            first.recover_runtime(RUN_ID), second.recover_runtime(RUN_ID)
        )
        assert first_state is not None and second_state is not None
        await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
            stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
        )
        results = await asyncio.gather(
            first.resume_recovered(first_state), second.resume_recovered(second_state)
        )
    finally:
        await first.aclose()
        await second.aclose()

    assert len(first_gateway.invocations) + len(second_gateway.invocations) == 1
    assert sum(result.actions_executed for result in results) == 1
    with sqlite3.connect(settings.checkpoint_path) as db:
        assert db.execute("SELECT count(*) FROM redteam_resume_claims").fetchone()[0] == 1


async def test_denied_durable_resume_stays_a_replan_and_never_executes(tmp_path) -> None:
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
        stopped.pending_approval_id, ApprovalStatus.REJECTED, "operator"
    )

    runner, _, gateway = _second_process(engagement, settings, approval_dir, audit_dir)
    try:
        runtime = await runner.recover_runtime(RUN_ID)
        assert runtime is not None
        final = await runner.resume_recovered(runtime)
    finally:
        await runner.aclose()

    assert gateway.invocations == []
    assert final.actions_executed == 0
    assert final.pending_approval_id is None
    # It went through POLICY -> UPDATE -> REPLAN (a real decision), rather
    # than being left exactly as the APPROVAL_PENDING stop it was.
    assert final.termination_reason != TerminationReason.APPROVAL_PENDING


async def test_durable_resume_requires_the_explicit_link_not_a_stale_guess(tmp_path) -> None:
    """Without ``register_pending_proposal`` the resolved Approval is stale:
    the run must resolve it to a fail-closed DENY, never execute."""
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )

    runner, _, gateway = _second_process(engagement, settings, approval_dir, audit_dir)
    try:
        runtime = await runner.recover_runtime(RUN_ID)
        assert runtime is not None
        result = await runner.reconcile_once(runtime, timeout_seconds=0.01)
    finally:
        await runner.aclose()

    assert result.resumed is True  # approval_outcome resolves...
    assert result.runtime.actions_executed == 0  # ...as a stale DENY
    assert gateway.invocations == []


async def test_recover_runtime_refuses_a_checkpoint_with_execution_pending() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=True))
    runtime = RuntimeState(
        run_id=RunId("run_unsafe"), engagement_id=EngagementId("eng_unsafe")
    )

    class _FakeGraph:
        def __init__(self, snapshot: object) -> None:
            self._snapshot = snapshot

        async def aget_state(self, config: object) -> object:  # noqa: ARG002
            return self._snapshot

    unsafe = AgentLoopRunner(deps, settings=deps.settings)
    unsafe._graph = _FakeGraph(  # noqa: SLF001 - focused replay-safety check
        SimpleNamespace(values={"runtime": runtime}, next=["execute"])
    )
    assert await unsafe.recover_runtime("run_unsafe") is None

    worker = AgentLoopRunner(deps, settings=deps.settings)
    worker._graph = _FakeGraph(  # noqa: SLF001
        SimpleNamespace(values={"runtime": runtime}, next=["worker"])
    )
    assert await worker.recover_runtime("run_unsafe") is None

    safe = AgentLoopRunner(deps, settings=deps.settings)
    safe._graph = _FakeGraph(  # noqa: SLF001
        SimpleNamespace(values={"runtime": runtime}, next=["verify"])
    )
    assert await safe.recover_runtime("run_unsafe") is runtime


async def test_recover_runtime_returns_none_for_an_unknown_run(tmp_path) -> None:
    settings = _durable_settings(tmp_path)
    deps = build_test_application(settings=settings)
    runner = AgentLoopRunner(deps, settings=settings)
    try:
        assert await runner.recover_runtime("run_does_not_exist") is None
    finally:
        await runner.aclose()


async def test_recover_runtime_needs_a_checkpointer() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    runner = AgentLoopRunner(deps, settings=deps.settings)

    with pytest.raises(CheckpointingDisabledError):
        await runner.recover_runtime("run_any")


async def test_resume_recovered_leaves_a_hard_stop_untouched() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=True))
    runner = AgentLoopRunner(deps, settings=deps.settings)
    runtime = RuntimeState(
        run_id=RunId("run_hard"),
        engagement_id=EngagementId("eng_hard"),
        termination_reason=TerminationReason.BUDGET_EXHAUSTED,
    )

    assert await runner.resume_recovered(runtime) is runtime
