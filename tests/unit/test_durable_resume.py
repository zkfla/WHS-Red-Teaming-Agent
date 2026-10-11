"""Durable resume: recover a checkpoint in a fresh process and resume safely.

Prompt 6 split from the in-process ``reconcile_once`` path:

    process 1: run -> APPROVAL_PENDING (durable sqlite checkpoint + Approval)
    process 2: recover_runtime(run_id) -> resume_recovered(runtime)

Only an explicitly re-linked checkpointed proposal may resume; a checkpoint
whose next super-step is EXECUTE/WORKER is never replayed; and a resolved
denial goes back through the REPLAN path rather than executing.

New durable contract (this migration):

- Every model answer comes from the in-file scripted model below, which
  speaks the genuine native ReAct contract (``complete_native`` with one
  real mobile tool call on the first turn, empty completions to hold)
  and is injected via an explicit ``llm_client``. No fake model with
  canned ``{}``, no rule fallback: an unscripted route raises instead
  of completing a turn.
- Every domain store is a SQLite file under ``tmp_path``: investigation
  paths, the work ledger, the mission/task graph, and the attack graph.
  The LangGraph checkpoint itself is SQLite too. Memory-only checkpointing
  alone does not satisfy this contract.
  owning engagement, plus the target proof (the declared asset node id in
  the path steps).
- The checkpointed proposal is checked against graph provenance that names
  the owning engagement, plus the target proof (the declared asset node id
  in the work ledger row and the proposal target) and the original provider
  call id shared by the native envelope, the plan step, and the work row.
  Native runs keep no investigation path; historical path durability is
  covered separately by a genuinely legacy PathPlan fixture below.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.sqlite import SQLiteAttackGraphStore
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
from redteam.core.enums import (
    AgentPhase,
    ApprovalStatus,
    GraphNodeType,
    MissionStatus,
    ToolHealthStatus,
)
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import MOBILE_AGENT, AgentRegistry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.local_tools import LocalToolRegistry
from redteam.orchestration.mission_bootstrap import bootstrap_mission
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
    goal_settle_descriptor,
    goal_settle_handler,
    task_settle_descriptor,
    task_settle_handler,
)
from redteam.orchestration.paths.models import (
    CompletionCriterion,
    PathPlan,
    PathStep,
    is_active_status,
)
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.runner import AgentLoopRunner, CheckpointingDisabledError
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.models import WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.fakes import DummyToolGateway, FakeEvidenceStore, build_test_application

RUN_ID = "run_durable"
ASSET_ID = "evil.apk"
ASSET_NODE_ID = "asset:evil.apk"
MOBILE_TOOL = "mobile.artifact_analyze"


class _ScriptedLLM:
    """Speak the genuine native ReAct contract (model -> tools -> settle).

    The first model turn of a run emits one genuine native call to the
    mobile tool through its provider-exposed name (``exposed_tool_name``,
    never hardcoded); every later turn holds with an empty completion.
    The approval gate suspends the first turn with ``APPROVAL_PENDING``,
    and the checkpoint already carries the assistant tool-calls message,
    so a resumed process never sees a first turn again and never re-emits
    the call: resume executes the checkpointed proposal exactly once with
    its original call id.

    ``complete_structured`` answers only the genuine review routes the
    execution path legitimately makes (analysis ``meaning`` plus the
    task/goal settlement reviews). Anything from the retired strategy
    contract raises so no rule fallback can ever complete a turn.
    """

    #: Stable provider call id for the scripted mobile call. The
    #: checkpointed proposal carries it, so approvals keep the original id.
    MOBILE_CALL_ID = "call-mobile-1"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0
        self._mobile_emitted = False

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    def _is_first_turn(self, messages: Any) -> bool:
        if self._mobile_emitted:
            return False
        for message in messages or []:
            if message.get("role") in ("assistant", "tool"):
                return False
        return True

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = {str(getattr(spec, "name", "")) for spec in tools}
        mobile = exposed_tool_name(MOBILE_TOOL)
        task_settle = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_settle = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert mobile in names, "native model needs the registered mobile tool"
        assert task_settle in names, "native model needs the task settlement helper"
        assert goal_settle in names, "native model needs the goal settlement helper"
        self.turns += 1
        if self._is_first_turn(messages):
            self._mobile_emitted = True
            self.calls.append({"turn": self.turns, "tool_calls": [mobile]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self.MOBILE_CALL_ID,
                        name=mobile,
                        arguments={},
                    ),
                ),
                text="",
            )
        self.calls.append({"turn": self.turns, "tool_calls": []})
        return NativeCompletion(tool_calls=(), text="")

    async def complete_structured(
        self,
        *,
        messages: Any,
        response_schema: dict[str, Any],
        system: str | None = "",
        num_retries: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.reviews.append(route)
        if route == "analysis_verify":
            return _meaning_success()
        if route == "task_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload["verified_source_ids"])
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "mobile tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            criteria = payload["mission"]["success_criteria"]
            receipts = list(payload["verified_source_ids"])
            assert receipts, "goal review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": item,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "mobile tool result observed",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for item in criteria
                ],
            }
        raise AssertionError(
            f"legacy strategy route is not part of the native loop: {route!r}"
        )

    async def complete(
        self, *, messages: Any, system: str | None = None, **_: Any
    ) -> str:
        return ""


def _meaning_success() -> dict[str, Any]:
    return {
        "meaning": "scripted",
        "outcome": "success",
        "reason": "scripted meaning",
        "confidence": 0.8,
        "claim_supported": True,
    }


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


def _native_tool_registry() -> LocalToolRegistry:
    """Task/goal settlement helpers offered to the native model."""
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return registry


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


def _files(directory) -> dict[str, str]:  # noqa: ANN001
    """Task and path state share one transaction; other durable stores are adjacent."""
    directory = Path(directory)
    return {
        "checkpoint": str(directory / "checkpoints.sqlite3"),
        "paths": str(directory / "task_graph.sqlite3"),
        "work": str(directory / "work.sqlite3"),
        "tasks": str(directory / "task_graph.sqlite3"),
        "graph": str(directory / "attack_graph.sqlite3"),
    }


def _durable_settings(tmp_path) -> OrchestrationSettings:  # noqa: ANN001
    files = _files(tmp_path)
    return OrchestrationSettings(
        checkpointing=True,
        checkpoint_backend="sqlite",
        checkpoint_path=files["checkpoint"],
        work_backend="sqlite",
        work_path=files["work"],
        task_graph_backend="sqlite",
        task_graph_path=files["tasks"],
        attack_graph_backend="sqlite",
        attack_graph_path=files["graph"],
        path_backend="sqlite",
        max_steps=10,
        thread_id=RUN_ID,
    )


def _plane(
    engagement: Engagement,
    approval_dir,
    audit_dir,  # noqa: ANN001
) -> DefaultControlPlane:
    return DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=AuditLog(FilesystemAuditStore(audit_dir)),
        approvals=ApprovalService(FilesystemApprovalStore(approval_dir)),
    )


async def _run_to_pending(tmp_path, *, max_actions: int = 5):  # noqa: ANN202
    """Process 1: scripted run to APPROVAL_PENDING on all-SQLite stores."""
    engagement = _engagement(max_actions=max_actions)
    approval_dir = tmp_path / "approvals"
    audit_dir = tmp_path / "audit"
    settings = _durable_settings(tmp_path)
    files = _files(tmp_path)
    plane = _plane(engagement, approval_dir, audit_dir)
    gateway = DummyToolGateway()
    llm = _ScriptedLLM()
    path_store = SQLitePathStore(files["paths"])
    work_store = SQLiteWorkStore(files["work"])
    task_store = SQLiteTaskGraphStore(files["tasks"])
    graph_store = SQLiteAttackGraphStore(files["graph"])
    await work_store.setup()
    await task_store.setup()
    await graph_store.setup()
    deps = RuntimeDeps(
        control_plane=plane,
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=graph_store,
        evidence_store=FakeEvidenceStore(),
        llm_client=llm,
        engagement=engagement,
        agents=AgentRegistry([MOBILE_AGENT]),
        tool_catalog=_mobile_catalog(),
        local_tools=_native_tool_registry(),
        work_store=work_store,
        task_graph_store=task_store,
        path_store=path_store,
        operator_intent_store=None,
        settings=settings,
    )
    await AttackGraphService(deps.graph_store).upsert_node(
        GraphNodeType.ASSET,
        key=ASSET_ID,
        attrs={"in_scope": True},
        facts={},
        labels=["engagement-scope"],
        provenance=Provenance(
            step=0,
            source="engagement:eng_durable",
            confidence=Confidence.SERVER_DECLARED,
        ),
        step=0,
    )
    mission_id = await bootstrap_mission(
        deps.task_graph_store,
        deps.agents,
        engagement,
        objective="analyze the declared artifact",
        now=datetime.now(timezone.utc),
    )
    mission = await deps.task_graph_store.get_mission(mission_id)
    assert mission is not None
    runner = AgentLoopRunner(deps, settings=settings)
    stopped = await runner.run(
        RuntimeState(
            run_id=RunId(RUN_ID),
            engagement_id=engagement.engagement_id,
            mission_id=mission_id,
            current_goal=mission.objective,
            remaining_budget=engagement.execution_budget.max_actions,
        )
    )
    await runner.aclose()
    assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
    assert stopped.pending_approval_id is not None
    return engagement, settings, approval_dir, audit_dir, stopped


def _second_process_with_deps(  # noqa: ANN202
    engagement: Engagement,
    settings: OrchestrationSettings,
    approval_dir,
    audit_dir,  # noqa: ANN001
):
    """Fresh process: reopen the same SQLite files, rescript the model tail."""
    files = _files(Path(settings.checkpoint_path).parent)
    plane = _plane(engagement, approval_dir, audit_dir)
    gateway = DummyToolGateway()
    llm = _ScriptedLLM()
    path_store = SQLitePathStore(files["paths"])
    work_store = SQLiteWorkStore(files["work"])
    task_store = SQLiteTaskGraphStore(files["tasks"])
    graph_store = SQLiteAttackGraphStore(files["graph"])
    deps = RuntimeDeps(
        control_plane=plane,
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=graph_store,
        evidence_store=FakeEvidenceStore(),
        llm_client=llm,
        engagement=engagement,
        agents=AgentRegistry([MOBILE_AGENT]),
        tool_catalog=_mobile_catalog(),
        local_tools=_native_tool_registry(),
        work_store=work_store,
        task_graph_store=task_store,
        path_store=path_store,
        operator_intent_store=None,
        settings=settings,
    )
    return AgentLoopRunner(deps, settings=settings), plane, gateway, deps


def _second_process(  # noqa: ANN202
    engagement: Engagement,
    settings: OrchestrationSettings,
    approval_dir,
    audit_dir,  # noqa: ANN001
):
    runner, plane, gateway, _ = _second_process_with_deps(
        engagement, settings, approval_dir, audit_dir
    )
    return runner, plane, gateway


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
    from redteam.app.cli.commands.resume import _read_checkpoint_identity

    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    hint = await _read_checkpoint_identity(settings, RUN_ID)

    assert hint is not None
    assert hint.engagement_id == engagement.engagement_id
    assert hint.component_binding == stopped.component_binding
    assert hint.mission_id == (str(stopped.mission_id) if stopped.mission_id else None)


async def test_approved_durable_resume_executes_the_original_action(tmp_path) -> None:
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    # Native pre-resume proof, read back on fresh objects: the checkpointed
    # proposal names the mobile tool and the declared asset, and the
    # original provider call id is shared by the native envelope, the plan
    # step, and the pending work row. Native calls intentionally carry no
    # investigation path, so no active PathPlan is expected here.
    files = _files(tmp_path)
    assert stopped.pending_action_id is not None
    assert stopped.active_path_id is None
    assert await SQLitePathStore(files["paths"]).get_active(RUN_ID) is None
    probe, _, _, probe_deps = _second_process_with_deps(
        engagement, settings, approval_dir, audit_dir
    )
    try:
        snapshot = await probe._latest_snapshot()  # noqa: SLF001
        assert snapshot is not None and snapshot.values is not None
        proposal = snapshot.values.get("action_proposal")
        assert proposal is not None
        assert proposal.action_id == stopped.pending_action_id
        assert str(proposal.run_id) == RUN_ID
        assert str(proposal.tool_id) == MOBILE_TOOL
        assert getattr(proposal.target, "asset_id", None) == ASSET_ID
        plan_step = snapshot.values.get("plan_step")
        assert plan_step is not None
        assert plan_step.model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID
        work_id = snapshot.values.get("current_work_id")
        assert work_id is not None
        envelope = snapshot.values.get("native_calls") or []
        assert len(envelope) == 1
        assert envelope[0].call_id == _ScriptedLLM.MOBILE_CALL_ID
        assert str(envelope[0].tool_id) == MOBILE_TOOL
        assert envelope[0].name == exposed_tool_name(MOBILE_TOOL)
        assert envelope[0].dispatch is not None
        assert envelope[0].dispatch.work_id == work_id
        pending_rows = await probe_deps.work_store.list_items(
            run_id=RunId(RUN_ID), status=WorkStatus.PENDING
        )
        assert len(pending_rows) == 1
        assert pending_rows[0].work_id == work_id
        assert pending_rows[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID
        assert [str(t) for t in pending_rows[0].target_node_ids] == [ASSET_NODE_ID]
        assert pending_rows[0].candidate_key == plan_step.candidate_key
        assistant_msgs = [
            message
            for message in (snapshot.values.get("native_messages") or [])
            if message.get("role") == "assistant"
        ]
        assert assistant_msgs
        assert any(
            _ScriptedLLM.MOBILE_CALL_ID in json.dumps(message, default=str)
            for message in assistant_msgs
        )
    finally:
        await probe.aclose()
    graph_store = SQLiteAttackGraphStore(files["graph"])
    await graph_store.setup()
    asset_nodes = [n for n in await graph_store.list_nodes() if n.type == GraphNodeType.ASSET]
    assert asset_nodes, "the seeded asset node survives in the durable graph"
    dumped_graph = json.dumps([n.model_dump(mode="json") for n in asset_nodes], default=str)
    assert "eng_durable" in dumped_graph
    assert ASSET_ID in dumped_graph
    # Process 1 is gone; the human approves through the durable store.
    await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )

    runner, _, gateway, deps = _second_process_with_deps(
        engagement, settings, approval_dir, audit_dir
    )
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
    # Executed-evidence proof: the single invocation carries the original
    # checkpointed action id, and the durable work row plus tool evidence
    # link back to that same action.
    assert len(gateway.invocations) == 1
    assert str(gateway.invocations[0].action_id) == str(stopped.pending_action_id)
    assert str(gateway.invocations[0].run_id) == RUN_ID
    succeeded = await deps.work_store.list_items(
        run_id=RunId(RUN_ID), status=WorkStatus.SUCCEEDED
    )
    assert len(succeeded) == 1
    assert succeeded[0].work_id == work_id
    assert succeeded[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID
    linked = await deps.evidence_store.list_for_action(stopped.pending_action_id)
    assert linked, "the executed original action leaves evidence behind"
    assert all(str(record.run_id) == RUN_ID for record in linked)
    assert any(
        str(record.invocation_id) == str(gateway.invocations[0].invocation_id)
        for record in linked
    )


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
    runtime = RuntimeState(run_id=RunId("run_unsafe"), engagement_id=EngagementId("eng_unsafe"))

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


async def test_sqlite_work_progress_survives_reopen(tmp_path) -> None:
    """Native step progress is durable: a fresh work-store object sees the
    pending row before resume, and after an approved resume fresh objects
    on the same SQLite files see that same row succeeded with the original
    provider call id."""
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    files = _files(tmp_path)

    assert stopped.active_path_id is None
    assert await SQLitePathStore(files["paths"]).get_active(RUN_ID) is None
    before = await SQLiteWorkStore(files["work"]).list_items(
        run_id=RunId(RUN_ID), status=WorkStatus.PENDING
    )
    assert len(before) == 1
    assert before[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID
    assert [str(t) for t in before[0].target_node_ids] == [ASSET_NODE_ID]

    task_reopened = SQLiteTaskGraphStore(files["tasks"])
    await task_reopened.setup()
    mission = await task_reopened.get_mission(stopped.mission_id)
    assert mission is not None
    assert mission.status is MissionStatus.ACTIVE

    await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )
    runner, _, gateway, deps = _second_process_with_deps(
        engagement, settings, approval_dir, audit_dir
    )
    try:
        runtime = await runner.recover_runtime(RUN_ID)
        assert runtime is not None
        final = await runner.resume_recovered(runtime)
    finally:
        await runner.aclose()

    assert [str(inv.tool_id) for inv in gateway.invocations] == [MOBILE_TOOL]
    assert final.actions_executed == 1
    assert str(gateway.invocations[0].action_id) == str(stopped.pending_action_id)
    succeeded = await deps.work_store.list_items(
        run_id=RunId(RUN_ID), status=WorkStatus.SUCCEEDED
    )
    assert len(succeeded) == 1
    assert succeeded[0].work_id == before[0].work_id
    assert succeeded[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID

    reopened = SQLiteWorkStore(files["work"])
    succeeded_again = await reopened.list_items(
        run_id=RunId(RUN_ID), status=WorkStatus.SUCCEEDED
    )
    assert [item.work_id for item in succeeded_again] == [before[0].work_id]
    assert succeeded_again[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID


async def test_durable_evidence_hides_secrets_but_keeps_linkage(tmp_path) -> None:
    """Tool output carries no secret material, yet every record still links
    back to its run and action, and the durable mission payload stays clean."""
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)

    await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )
    runner, _, _, deps = _second_process_with_deps(engagement, settings, approval_dir, audit_dir)
    try:
        runtime = await runner.recover_runtime(RUN_ID)
        assert runtime is not None
        final = await runner.resume_recovered(runtime)
    finally:
        await runner.aclose()

    records = await deps.evidence_store.list_for_run(RunId(RUN_ID))
    assert records, "the executed action leaves evidence behind"
    assert all(str(r.run_id) == RUN_ID for r in records)
    assert any(r.action_id is not None for r in records)
    dumped = json.dumps([r.model_dump(mode="json") for r in records], default=str).lower()
    for marker in ("api_key", "password", "passwd", "private_key", "client_secret"):
        assert marker not in dumped

    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None
    assert "api_key" not in json.dumps(mission.model_dump(mode="json"), default=str).lower()


async def test_legacy_checkpoint_without_recent_observations_recovers_and_resumes(
    tmp_path,
) -> None:
    """Old 40aedb2 payload (no recent_observations) recovers on the new runner."""
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    files = _files(tmp_path)
    assert stopped.pending_approval_id is not None
    assert stopped.pending_action_id is not None
    assert stopped.mission_id is not None
    before_work = await SQLiteWorkStore(files["work"]).list_items(
        run_id=RunId(RUN_ID), status=WorkStatus.PENDING
    )
    assert len(before_work) == 1
    assert before_work[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID
    new_dump = stopped.model_dump()
    assert "recent_observations" in new_dump
    old_dict = dict(new_dump)
    old_dict.pop("recent_observations", None)
    assert "recent_observations" not in old_dict
    legacy_runtime = stopped.model_copy()
    legacy_runtime.__dict__.pop("recent_observations", None)
    assert "recent_observations" not in legacy_runtime.__dict__
    legacy_blob = legacy_runtime.model_dump()
    assert "recent_observations" not in legacy_blob
    revalidated = RuntimeState.model_validate(old_dict)
    assert isinstance(revalidated, RuntimeState)
    assert revalidated.recent_observations == []
    assert revalidated.run_id == stopped.run_id
    assert revalidated.pending_approval_id == stopped.pending_approval_id
    writer, _, _, _ = _second_process_with_deps(engagement, settings, approval_dir, audit_dir)
    try:
        await writer._ensure_graph()
        assert writer._graph is not None
        snapshot_before = await writer._graph.aget_state(writer.config())
        assert snapshot_before.values is not None
        assert snapshot_before.values.get("runtime") is not None
        proposal_before = snapshot_before.values.get("action_proposal")
        assert proposal_before is not None
        work_id_before = snapshot_before.values.get("current_work_id")
        assert work_id_before is not None
        update: dict[str, object] = {"runtime": legacy_runtime}
        plan_step_before = snapshot_before.values.get("plan_step")
        if plan_step_before is not None:
            update["plan_step"] = plan_step_before
        update["action_proposal"] = proposal_before
        update["current_work_id"] = work_id_before
        await writer._graph.aupdate_state(writer.config(), update)
    finally:
        await writer.aclose()
    runner, _, gateway = _second_process(engagement, settings, approval_dir, audit_dir)
    try:
        recovered = await runner.recover_runtime(RUN_ID)
        assert recovered is not None
        assert isinstance(recovered, RuntimeState)
        assert recovered.run_id == stopped.run_id
        assert recovered.engagement_id == stopped.engagement_id
        assert recovered.mission_id == stopped.mission_id
        assert recovered.active_task_id == stopped.active_task_id
        assert recovered.active_path_id == stopped.active_path_id
        assert recovered.pending_approval_id == stopped.pending_approval_id
        assert recovered.pending_action_id == stopped.pending_action_id
        assert recovered.recent_observations == []
        assert await SQLitePathStore(files["paths"]).get_active(RUN_ID) is None
        still_pending = await SQLiteWorkStore(files["work"]).list_items(
            run_id=RunId(RUN_ID), status=WorkStatus.PENDING
        )
        assert [item.work_id for item in still_pending] == [
            item.work_id for item in before_work
        ]
        await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
            stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
        )
        final = await runner.resume_recovered(recovered)
    finally:
        await runner.aclose()
    assert final.run_id == stopped.run_id
    assert final.actions_executed == 1
    assert [str(inv.tool_id) for inv in gateway.invocations] == ["mobile.artifact_analyze"]
    assert str(gateway.invocations[0].action_id) == str(stopped.pending_action_id)
    done = await SQLiteWorkStore(files["work"]).list_items(
        run_id=RunId(RUN_ID), status=WorkStatus.SUCCEEDED
    )
    assert [item.work_id for item in done] == [item.work_id for item in before_work]
    assert done[0].model_tool_call_id == _ScriptedLLM.MOBILE_CALL_ID


def _legacy_path_plan() -> PathPlan:
    """Genuinely legacy investigation path: an accepted/active PathPlan aimed
    at the declared asset, of the kind historical strategy-loop runs stored."""
    return PathPlan(
        path_id="path_legacy_durable",
        run_id="run_legacy_path",
        mission_id="mission_legacy",
        version=1,
        parent_version=None,
        status="active",
        reason="historical accepted investigation path",
        goal="analyze the declared artifact",
        steps=[
            PathStep(
                step_id="step-1",
                objective="analyze the declared APK artifact",
                agent_id="mobile-agent",
                target_node_ids=[ASSET_NODE_ID],
                completion_criteria=[
                    CompletionCriterion(
                        kind="evidence", description="static analysis evidence recorded",
                        min_evidence=1,
                    )
                ],
            )
        ],
    )


async def test_sqlite_legacy_path_plan_survives_reopen(tmp_path) -> None:
    """Historical path durability, independent of the native loop: a legacy
    active PathPlan registered on SQLite is still active after reopen, and
    step evidence recorded on it survives one more reopen."""
    files = _files(tmp_path)
    registered = await SQLitePathStore(files["paths"]).register(_legacy_path_plan())
    assert is_active_status(registered.status)

    reopened = await SQLitePathStore(files["paths"]).get_active("run_legacy_path")
    assert reopened is not None
    assert reopened.path_id == registered.path_id
    assert is_active_status(reopened.status)
    assert any(
        ASSET_NODE_ID in [str(t) for t in step.target_node_ids]
        for step in reopened.steps
    )

    progressed = await SQLitePathStore(files["paths"]).update_step(
        "run_legacy_path",
        registered.path_id,
        registered.version,
        "step-1",
        status="completed",
        outcome="static analysis evidence recorded",
        evidence_ids=["ev_legacy_1"],
    )
    assert progressed is not None

    again = await SQLitePathStore(files["paths"]).get_active("run_legacy_path")
    assert again is not None
    assert again.path_id == registered.path_id
    assert [(s.step_id, s.status, list(s.evidence_ids)) for s in again.steps] == [
        (s.step_id, s.status, list(s.evidence_ids)) for s in progressed.steps
    ]
    assert again.steps[0].evidence_ids == ["ev_legacy_1"]
