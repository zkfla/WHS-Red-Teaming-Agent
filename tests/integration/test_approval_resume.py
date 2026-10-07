"""Approval resume through the real engine contract (scripted native LLM, mobile asset).

Plan: one genuine native mobile tool call on a declared asset, then the
run suspends at REQUIRE_APPROVAL and continues only via reconcile_once()
at POLICY with approval_outcome(). No rule fallback ever completes a turn:
model turns come from complete_native below and verification answers come
from the genuine analysis_verify review.
"""

from __future__ import annotations

import json
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
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
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
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkStatus
from tests.integration import _engine_llm_harness as h

ASSET_ID = "evil.apk"
ASSET_NODE_ID = "asset:evil.apk"
MOBILE_AGENT_ID = "mobile-agent"
MOBILE_TOOL = "mobile.artifact_analyze"
MOBILE_CAPABILITY = "mobile"
MOBILE_OPERATION = "static_analysis"
MOBILE_OP_KEY = "mobile.static_analysis"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _native_tool_registry() -> LocalToolRegistry:
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return registry


def _exposed_names(tools: list[Any]) -> set[str]:
    return {str(getattr(spec, "name", "")) for spec in tools}


class _ScriptedNativeLLM:
    """One genuine native mobile call, then hold. Reviews stay genuine."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0

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
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "analysis_verify":
            return h.meaning("success", True)
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
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        mobile = exposed_tool_name(MOBILE_TOOL)
        task_settle = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_settle = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert mobile in names, "native model needs the registered mobile tool"
        assert task_settle in names, "native model needs the task settlement helper"
        assert goal_settle in names, "native model needs the goal settlement helper"
        self.turns += 1
        if self.turns == 1:
            self.calls.append({"turn": self.turns, "tool_calls": [mobile]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-mobile-1",
                        name=mobile,
                        arguments={"apk_ref": ASSET_ID},
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")

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

    @property
    def invocations(self) -> list[Any]:
        return self.calls

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


async def _setup(*, max_actions: int = 5, checkpoint_path: str | None = None):
    engagement = _engagement(max_actions=max_actions)
    llm = _ScriptedNativeLLM()
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
        local_tools=_native_tool_registry(),
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
