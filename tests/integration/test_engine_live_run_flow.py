"""Live engine-flow tests: real native graph run, real control plane, counted gateway.

The scripted model answers genuine native tool calls; everything else (the
model -> tools -> settle loop, mission bootstrap, settlement) is production
code. No network, no clock tricks. The suite itself is never executed here.
"""

from __future__ import annotations

import json
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import ExecutionBudget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.local_tools import LocalToolRegistry
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
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.integration import _engine_llm_harness as h


def _native_tool_registry() -> LocalToolRegistry:
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return registry


def _exposed_names(tools: list[Any]) -> set[str]:
    return {str(getattr(spec, "name", "")) for spec in tools}


def _remote_executed_evidence(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or not message.get("tool_call_id"):
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("ok") is not True:
            continue
        if body.get("executed") is not True:
            continue
        for identity in body.get("evidence_ids") or []:
            text = str(identity)
            if text and text not in found:
                found.append(text)
    return found


def _tool_result(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


class CountingGateway:
    """Tool gateway double that counts real invocations and answers success."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(self, invocation: Any) -> ToolResult:
        self.calls.append(invocation)
        now = h.utcnow()
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=True,
            output={"inventory": "ok", "host": h.HOST},
            started_at=now,
            finished_at=now,
            metadata={},
        )


class LiveScriptedLLM:
    """Native scripted model: one inventory call, then task/goal settlement.

    Speaks the genuine product contract: ``complete_native`` with
    ``NativeCompletion``/``NativeToolCall`` payloads addressed to the exposed
    tool names, plus the preserved ``complete_structured`` reviews for
    verification, task settlement, and goal settlement. Any legacy strategy
    route raises instead of returning a soft answer.
    """

    def __init__(self, *, graph_store: Any, evidence_store: Any) -> None:
        self._graph = graph_store
        self._evidence = evidence_store
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0
        self._task_call: str | None = None
        self._goal_call: str | None = None

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
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        num_retries: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.reviews.append(route)
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
                        "description": "inventory tool result observed",
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
                                "description": "inventory tool result observed",
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
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        inventory = exposed_tool_name(h.INVENTORY_TOOL)
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert inventory in names, "native model needs the registered inventory tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        self.calls.append({"turn": self.turns, "evidenced": list(evidenced)})
        if self.turns == 1:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(call_id="call-live-1", name=inventory, arguments={}),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-live-task"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "task evidence ready",
                            "evidence_ids": list(evidenced),
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-live-goal"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "goal evidence ready",
                            "evidence_ids": list(evidenced),
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


def _live_deps(llm: LiveScriptedLLM, gateway: CountingGateway, engagement: Any) -> RuntimeDeps:
    catalog = h.FakeCatalog([h.inventory_descriptor()])
    return RuntimeDeps(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement},
            outbound_descriptor_resolver=catalog.get,
        ),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=llm._graph,
        evidence_store=llm._evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=catalog,
        local_tools=_native_tool_registry(),
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(
            max_steps=8, checkpointing=False, decision_max_attempts=6
        ),
    )


async def test_live_run_reaches_step_and_mission_completion_with_one_call():
    """Real native loop: 1 counted execution, then task + mission settlement."""
    llm = LiveScriptedLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
    )
    gateway = CountingGateway()
    engagement = h.make_engagement(engagement_id="eng-live")
    deps = _live_deps(llm, gateway, engagement)

    runtime = await run_engagement(engagement, deps)

    assert len(gateway.calls) == 1
    assert str(gateway.calls[0].tool_id) == h.INVENTORY_TOOL
    mission = await deps.task_graph_store.get_mission(runtime.mission_id)
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED
    assert runtime.termination_reason == TerminationReason.GOAL_ACHIEVED
    tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
    settled = [task for task in tasks if task.status is TaskStatus.COMPLETED]
    assert settled, "task settlement keeps its completed task"
    assert all(task.objective == mission.objective for task in settled)
    tool_ids = {
        str(record.evidence_id)
        for record in await deps.evidence_store.list_for_run(runtime.run_id)
        if isinstance(getattr(record, "metadata", {}).get("tool_result"), dict)
    }
    assert tool_ids, "settlement needs the executed call evidence"
    assert mission.criterion_results, "goal settlement keeps its evidence results"
    for result in mission.criterion_results:
        assert result.met and result.evidence_ids
        assert set(map(str, result.evidence_ids)) <= tool_ids
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    assert any(isinstance(r.metadata.get("tool_result"), dict) for r in records)
    assert {"task_completion_review", "goal_completion_review"} <= set(llm.reviews)


async def test_live_run_with_single_allowed_call_closes_the_goal():
    """max_actions=1: one real call, then native settlement closes the goal."""
    llm = LiveScriptedLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
    )
    gateway = CountingGateway()
    engagement = h.make_engagement(engagement_id="eng-live-one").model_copy(
        update={
            "execution_budget": ExecutionBudget(max_actions=1),
        }
    )
    deps = _live_deps(llm, gateway, engagement)

    runtime = await run_engagement(engagement, deps)

    assert len(gateway.calls) == 1
    assert str(gateway.calls[0].tool_id) == h.INVENTORY_TOOL
    mission = await deps.task_graph_store.get_mission(runtime.mission_id)
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED
    assert runtime.termination_reason == TerminationReason.GOAL_ACHIEVED
    tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
    settled = [task for task in tasks if task.status is TaskStatus.COMPLETED]
    assert settled, "single-call budget still settles its task"
    assert all(task.objective == mission.objective for task in settled)
    tool_ids = {
        str(record.evidence_id)
        for record in await deps.evidence_store.list_for_run(runtime.run_id)
        if isinstance(getattr(record, "metadata", {}).get("tool_result"), dict)
    }
    assert tool_ids, "settlement needs the executed call evidence"
    assert mission.criterion_results, "goal settlement keeps its evidence results"
    for result in mission.criterion_results:
        assert result.met and result.evidence_ids
        assert set(map(str, result.evidence_ids)) <= tool_ids
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    assert any(isinstance(r.metadata.get("tool_result"), dict) for r in records)
    assert {"task_completion_review", "goal_completion_review"} <= set(llm.reviews)
