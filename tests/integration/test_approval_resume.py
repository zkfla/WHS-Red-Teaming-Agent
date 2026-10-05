"""Approval resume through the real engine contract (scripted LLM, mobile asset).

Plan: draft -> review -> one mobile action on a declared asset, then the
run suspends at REQUIRE_APPROVAL and continues only via reconcile_once()
at POLICY with approval_outcome(). No rule fallback ever completes a turn:
every structured answer comes from the in-file script below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
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
from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import (
    ApprovalStatus,
    GraphNodeType,
    PolicyVerdict,
    RiskLevel,
    ToolHealthStatus,
)
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId, ToolId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.mission_bootstrap import bootstrap_mission
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkStatus

ASSET_ID = "evil.apk"
ASSET_NODE_ID = "asset:evil.apk"
MOBILE_AGENT_ID = "mobile-agent"
MOBILE_TOOL = "mobile.artifact_analyze"
MOBILE_CAPABILITY = "mobile"
MOBILE_OPERATION = "static_analysis"
MOBILE_OP_KEY = "mobile.static_analysis"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _ScriptedLLM:
    def __init__(self) -> None:
        self._routes: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, route: str, response: Any) -> None:
        self._routes.setdefault(route, []).append(response)

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
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


class _FakeEvidenceStore:
    def __init__(self) -> None:
        self._records: dict[str, Any] = {}

    async def append(self, evidence: Any) -> None:
        self._records[str(evidence.evidence_id)] = evidence

    async def get(self, evidence_id: Any) -> Any | None:
        return self._records.get(str(evidence_id))

    async def list_for_run(self, run_id: Any) -> list[Any]:
        return [r for r in self._records.values() if str(r.run_id) == str(run_id)]

    async def list_for_action(self, action_id: Any) -> list[Any]:
        return [r for r in self._records.values() if str(r.action_id) == str(action_id)]


class _FakeCatalog:
    def __init__(self, tools: list[ToolDescriptor]) -> None:
        self._tools = {str(t.tool_id): t for t in tools}

    def get(self, tool_id: Any) -> ToolDescriptor | None:
        return self._tools.get(str(tool_id))

    def all_descriptors(self) -> list[ToolDescriptor]:
        return list(self._tools.values())

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return [t for t in self._tools.values() if capability_tag in t.capability_tags]

    def list_tools_for_action(self, capability: str, operation: str) -> list[ToolDescriptor]:
        return [
            t
            for t in self._tools.values()
            if capability in t.capability_tags and operation in t.operations
        ]


class _CountingGateway:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(self, invocation: Any) -> ToolResult:
        self.calls.append(invocation)
        now = utcnow()
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=True,
            output={"artifact": "ok", "asset": ASSET_ID},
            started_at=now,
            finished_at=now,
            metadata={},
        )


def _mobile_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(MOBILE_TOOL),
        server_id="test-mobile",
        tool_name="artifact_analyze",
        description="test-only mobile static analysis with legal artifact refs",
        input_schema={
            "type": "object",
            "properties": {
                "apk_ref": {"type": "string"},
                "artifact_ref": {"type": "string"},
                "asset_ref": {"type": "string"},
            },
            "required": ["apk_ref"],
            "additionalProperties": True,
        },
        operations=[MOBILE_OPERATION],
        capability_tags=[MOBILE_CAPABILITY],
        target_types=["asset"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )


def _engagement(*, max_actions: int = 5) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_mobile"),
        name="mobile approval engine",
        scope=EngagementScope(allowed_assets=[ASSET_ID], allowed_hosts=["anything"]),
        execution_budget=ExecutionBudget(max_actions=max_actions),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(require_approval_for=[MOBILE_CAPABILITY]),
        created_at=utcnow(),
    )


def _draft_path(asset_node_id: str) -> dict[str, Any]:
    # Minimal model draft: purpose + steps only. Runtime identity
    # (path_id/run_id/version/status/goal/evidence) is code-owned.
    return {
        "reason": "scripted mobile draft",
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
            }
        ],
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


async def _setup(*, max_actions: int = 5, checkpoint_path: str | None = None):
    engagement = _engagement(max_actions=max_actions)
    llm = _ScriptedLLM()
    graph_store = InMemoryAttackGraphStore()
    evidence_store = _FakeEvidenceStore()
    path_store = InMemoryPathStore()
    work_store = InMemoryWorkStore()
    task_store = InMemoryTaskGraphStore()
    event_bus = InMemoryEventBus()
    control_plane = DefaultControlPlane({engagement.engagement_id: engagement})
    gateway = _CountingGateway()
    settings = OrchestrationSettings(
        checkpointing=True,
        max_steps=10,
        checkpoint_backend="sqlite" if checkpoint_path else "memory",
        checkpoint_path=checkpoint_path or "data/orchestration/checkpoints.sqlite3",
    )
    deps = RuntimeDeps(
        control_plane=control_plane,
        tool_gateway=gateway,
        event_bus=event_bus,
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=_FakeCatalog([_mobile_descriptor()]),
        work_store=work_store,
        task_graph_store=task_store,
        path_store=path_store,
        operator_intent_store=None,
        settings=settings,
    )
    assert deps.engagement is engagement
    await AttackGraphService(graph_store).upsert_node(
        GraphNodeType.ASSET,
        key=ASSET_ID,
        attrs={"in_scope": True},
        facts={},
        labels=["engagement-scope"],
        provenance=Provenance(
            step=0,
            source="engagement:eng_mobile",
            confidence=Confidence.SERVER_DECLARED,
        ),
        step=0,
    )
    mission_id = await bootstrap_mission(
        task_store, deps.agents, engagement, objective="analyze the declared artifact", now=utcnow()
    )
    mission = await task_store.get_mission(mission_id)
    assert mission is not None
    initial = RuntimeState(
        run_id=RunId("run_mobile"),
        engagement_id=engagement.engagement_id,
        mission_id=mission_id,
        current_goal=mission.objective,
        remaining_budget=engagement.execution_budget.max_actions,
    )
    llm.add("path_draft", _draft_path(ASSET_NODE_ID))
    llm.add("path_review", _review_accept())
    llm.add("next_action", _turn_mobile_action(ASSET_NODE_ID))
    llm.add("next_action", _turn_hold())
    llm.add("analysis_verify", _meaning_success())
    runner = AgentLoopRunner(deps, settings=deps.settings)
    return engagement, control_plane, gateway, deps, runner, initial


async def test_require_approval_then_approve_then_allow_and_execute() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup()

    stopped = await runner.run(initial)
    assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
    assert stopped.pending_approval_id is not None
    assert stopped.actions_executed == 0
    assert gateway.calls == []

    await control_plane.resolve_approval(
        stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
    )
    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True
    assert result.runtime.actions_executed == 1
    assert result.runtime.run_id == initial.run_id
    assert result.runtime.pending_approval_id is None
    assert [str(inv.tool_id) for inv in gateway.calls] == [MOBILE_TOOL]
    succeeded = await deps.work_store.list_items(run_id=initial.run_id, status=WorkStatus.SUCCEEDED)
    assert len(succeeded) == 1
    blocked = await deps.work_store.list_items(run_id=initial.run_id, status=WorkStatus.BLOCKED)
    assert len(blocked) == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_new_runner_resumes_original_approved_action_once(backend, tmp_path) -> None:
    _, control, gateway, deps, runner, initial = await _setup(
        checkpoint_path=str(tmp_path / "checkpoint.sqlite3") if backend == "sqlite" else None
    )
    stopped = await runner.run(initial)
    assert stopped.pending_approval_id is not None and gateway.calls == []
    await control.resolve_approval(stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator")
    # A restarted process must use the shared durable checkpoint/store, not
    # rely on the original Runner's in-memory thread binding.
    resumed_runner = AgentLoopRunner(
        deps,
        settings=deps.settings,
        checkpointer=runner._checkpointer if backend == "memory" else None,
    )
    try:
        result = await resumed_runner.reconcile_once(stopped, timeout_seconds=0.01)
        assert result.resumed and result.runtime.actions_executed == 1
        assert len(gateway.calls) == 1
        assert gateway.calls[0].action_id == stopped.pending_action_id
        assert gateway.calls[0].arguments["apk_ref"] == ASSET_ID
        # An old runtime is not permission to replay the same approved action.
        await resumed_runner.reconcile_once(stopped, timeout_seconds=0.01)
        assert len(gateway.calls) == 1
    finally:
        await resumed_runner.aclose()
        await runner.aclose()


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
    assert gateway.calls == []
    work = await deps.work_store.list_items(run_id=initial.run_id, status=WorkStatus.BLOCKED)
    assert len(work) == 1


async def test_duplicate_resolution_is_rejected() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup()
    stopped = await runner.run(initial)
    approval_id = stopped.pending_approval_id
    assert approval_id is not None
    await control_plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")

    with pytest.raises(ValueError, match="already"):
        await control_plane.resolve_approval(approval_id, ApprovalStatus.REJECTED, "operator_two")

    decision = await control_plane.approval_outcome(approval_id)
    assert decision is not None
    assert decision.verdict == PolicyVerdict.ALLOW


async def test_stale_approval_never_resumes_a_run_it_cannot_safely_identify() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup()
    stopped = await runner.run(initial)
    approval_id = stopped.pending_approval_id
    assert approval_id is not None

    await control_plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")
    control_plane._pending_proposals.pop(approval_id, None)
    control_plane._resolved_outcomes.pop(approval_id, None)

    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True
    assert result.runtime.actions_executed == 0
    assert gateway.calls == []


async def test_reevaluation_denies_when_budget_exhausted_meanwhile() -> None:
    engagement, control_plane, gateway, deps, runner, initial = await _setup(max_actions=1)
    stopped = await runner.run(initial)
    approval_id = stopped.pending_approval_id
    assert approval_id is not None

    other = ActionProposal(
        action_id=ActionId("action_other"),
        run_id=initial.run_id,
        engagement_id=engagement.engagement_id,
        agent_id=AgentId("agent_1"),
        capability="recon",
        operation="port_scan",
        target=HostTarget(host="anything"),
        reason="consume budget",
        created_at=utcnow(),
    )
    other_decision = await control_plane.evaluate(
        other, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert other_decision.verdict == PolicyVerdict.ALLOW

    await control_plane.resolve_approval(approval_id, ApprovalStatus.APPROVED, "operator")
    result = await runner.reconcile_once(stopped, timeout_seconds=0.05)

    assert result.resumed is True
    assert result.runtime.actions_executed == 0
    assert gateway.calls == []
