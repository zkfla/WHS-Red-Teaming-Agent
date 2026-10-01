"""Durable resume: recover a checkpoint in a fresh process and resume safely.

Prompt 6 split from the in-process ``reconcile_once`` path:

    process 1: run -> APPROVAL_PENDING (durable sqlite checkpoint + Approval)
    process 2: recover_runtime(run_id) -> resume_recovered(runtime)

Only an explicitly re-linked checkpointed proposal may resume; a checkpoint
whose next super-step is EXECUTE/WORKER is never replayed; and a resolved
denial goes back through the REPLAN path rather than executing.

New durable contract (this migration):

- Every structured answer comes from the in-file scripted model below,
  reused from tests/integration/test_approval_resume.py and injected via an
  explicit ``llm_client``. No fake model with canned ``{}``, no rule
  fallback: an unscripted route raises instead of completing a turn.
- Every domain store is a SQLite file under ``tmp_path``: investigation
  paths, the work ledger, the mission/task graph, and the attack graph.
  The LangGraph checkpoint itself is SQLite too. Memory-only checkpointing
  alone does not satisfy this contract.
- The accepted path is checked against graph provenance that names the
  owning engagement, plus the target proof (the declared asset node id in
  the path steps).
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
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import MOBILE_AGENT, AgentRegistry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.mission_bootstrap import bootstrap_mission
from redteam.orchestration.paths.models import is_active_status
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
MOBILE_AGENT_ID = "mobile-agent"
MOBILE_TOOL = "mobile.artifact_analyze"
MOBILE_CAPABILITY = "mobile"
MOBILE_OPERATION = "static_analysis"
MOBILE_OP_KEY = "mobile.static_analysis"


class _ScriptedLLM:
    """Answer each structured call from a per-route script, in order.

    Reused from tests/integration/test_approval_resume.py: the same route
    keys and the same mobile-asset payloads below. An unscripted route
    raises ``AssertionError`` so no rule fallback can ever complete a turn.
    """

    def __init__(self) -> None:
        self._routes: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, route: str, response: Any) -> None:
        self._routes.setdefault(route, []).append(response)

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "path_id" in props and "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

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
        self.calls.append({"route": route, "messages": messages, "system": system})
        queue = self._routes.get(route, [])
        if not queue:
            raise AssertionError(f"no scripted response left for route {route!r}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def complete(self, *, messages: Any, system: str | None = None, **_: Any) -> str:
        return ""


def _draft_path(asset_node_id: str) -> dict[str, Any]:
    return {
        "path_id": "path-draft-1",
        "run_id": "run-x",
        "mission_id": None,
        "version": 1,
        "parent_version": None,
        "status": "draft",
        "reason": "scripted mobile draft",
        "goal": "analyze the declared mobile artifact",
        "steps": [
            {
                "step_id": "step-1",
                "objective": "analyze the declared APK artifact",
                "agent_id": MOBILE_AGENT_ID,
                "target_node_ids": [asset_node_id],
                "principal_refs": [],
                "allowed_operations": [MOBILE_OP_KEY],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {
                        "kind": "evidence",
                        "description": "static analysis evidence recorded",
                        "min_evidence": 1,
                    }
                ],
                "required": True,
                "status": "pending",
                "outcome": None,
                "evidence_ids": [],
            }
        ],
        "hypothesis_ids": [],
        "evidence_ids": [],
        "changed_step_ids": [],
        "review_records": {},
    }


def _review_accept() -> dict[str, Any]:
    return {"decision": "accept", "reason": "scripted accept", "path": None}


def _turn_mobile_action(asset_node_id: str) -> dict[str, Any]:
    return {
        "mode": "actions",
        "reason": "scripted mobile action",
        "actions": [
            {
                "path_step_id": "step-1",
                "agent_id": MOBILE_AGENT_ID,
                "target_node_id": asset_node_id,
                "principal_ref": None,
                "capability": MOBILE_CAPABILITY,
                "operation": MOBILE_OPERATION,
                "tool_id": MOBILE_TOOL,
                "arguments": {},
                "reason": "analyze the declared artifact",
                "expected_observation": "static analysis returns artifact structure",
                "hypothesis": None,
                "existing_hypothesis_id": None,
                "expectation": None,
                "authentication_check": None,
                "availability_impact": False,
            }
        ],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def _turn_hold() -> dict[str, Any]:
    return {
        "mode": "hold",
        "reason": "scripted hold after the approval turn",
        "actions": [],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


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
    """One SQLite file per durable store, all next to each other."""
    directory = Path(directory)
    return {
        "checkpoint": str(directory / "checkpoints.sqlite3"),
        "paths": str(directory / "paths.sqlite3"),
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
    llm.add("path_draft", _draft_path(ASSET_NODE_ID))
    llm.add("path_review", _review_accept())
    llm.add("next_action", _turn_mobile_action(ASSET_NODE_ID))
    llm.add("next_action", _turn_hold())
    llm.add("analysis_verify", _meaning_success())
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
    llm.add("next_action", _turn_hold())
    llm.add("next_action", _turn_hold())
    llm.add("analysis_verify", _meaning_success())
    llm.add("analysis_verify", _meaning_success())
    llm.add("path_review", _review_accept())
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
    from redteam.app.cli.commands.resume import _read_checkpoint_runtime

    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    hint = await _read_checkpoint_runtime(settings, RUN_ID)

    assert hint is not None
    assert hint.engagement_id == engagement.engagement_id
    assert hint.run_id == stopped.run_id
    assert hint.termination_reason == TerminationReason.APPROVAL_PENDING
    assert hint.pending_approval_id == stopped.pending_approval_id


async def test_approved_durable_resume_executes_the_original_action(tmp_path) -> None:
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    # Accepted path: still active in the durable file, aimed at the declared
    # asset, and the graph provenance names the owning engagement.
    files = _files(tmp_path)
    active = await SQLitePathStore(files["paths"]).get_active(RUN_ID)
    assert active is not None
    assert is_active_status(active.status)
    assert any(ASSET_NODE_ID in [str(t) for t in step.target_node_ids] for step in active.steps)
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


async def test_sqlite_active_path_and_step_progress_survive_reopen(tmp_path) -> None:
    """Real SQLite path resume: fresh objects on the same files see the
    active path, and the step progress made by an approved resume is still
    there after one more reopen."""
    engagement, settings, approval_dir, audit_dir, stopped = await _run_to_pending(tmp_path)
    files = _files(tmp_path)

    before = await SQLitePathStore(files["paths"]).get_active(RUN_ID)
    assert before is not None
    assert is_active_status(before.status)
    assert any(ASSET_NODE_ID in [str(t) for t in step.target_node_ids] for step in before.steps)

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
    progressed = await deps.path_store.get_active(RUN_ID)
    assert progressed is not None
    assert progressed.path_id == before.path_id
    succeeded = await deps.work_store.list_items(run_id=RunId(RUN_ID), status=WorkStatus.SUCCEEDED)
    assert len(succeeded) == 1

    again = await SQLitePathStore(files["paths"]).get_active(RUN_ID)
    assert again is not None
    assert again.path_id == progressed.path_id
    assert [(s.step_id, list(s.evidence_ids)) for s in again.steps] == [
        (s.step_id, list(s.evidence_ids)) for s in progressed.steps
    ]


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
    before_path = await SQLitePathStore(files["paths"]).get_active(RUN_ID)
    assert before_path is not None
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
        active = await SQLitePathStore(files["paths"]).get_active(RUN_ID)
        assert active is not None
        assert active.path_id == before_path.path_id
        await ApprovalService(FilesystemApprovalStore(approval_dir)).resolve(
            stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
        )
        final = await runner.resume_recovered(recovered)
    finally:
        await runner.aclose()
    assert final.run_id == stopped.run_id
    assert final.actions_executed == 1
    assert [str(inv.tool_id) for inv in gateway.invocations] == ["mobile.artifact_analyze"]
