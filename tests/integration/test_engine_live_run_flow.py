"""Live engine-flow tests: real graph run, real control plane, counted gateway.

The scripted model only answers; everything else (the plan/policy/execute/
verify/update/replan loop, mission bootstrap, settlement) is production code.
No network, no clock tricks. The suite itself is never executed here.
"""

from __future__ import annotations

import json
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import ExecutionBudget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType, MissionStatus
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.integration import _engine_llm_harness as h


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


class LiveScriptedLLM(h.ScriptedLLM):
    """Static answers for fixed routes, store-driven answers for completion."""

    def __init__(self, *, graph_store: Any, evidence_store: Any) -> None:
        super().__init__()
        self._graph = graph_store
        self._evidence = evidence_store
        self._actions_calls = 0

    async def _host_node_id(self) -> str:
        nodes = await self._graph.list_nodes()
        host = next(n for n in nodes if n.type is GraphNodeType.HOST)
        return str(host.id)

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

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
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "path_draft":
            return h.draft_path(await self._host_node_id())
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
            payload = self._payload(messages)
            if isinstance(payload.get("task"), dict):
                excerpts = payload.get("evidence_excerpts", [])
                task_ids = [
                    str(item.get("evidence_id", "")).strip()
                    for item in excerpts
                    if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
                ]
                assert task_ids, "task review needs related stored evidence"
                return h.task_accept(task_ids, reason="inventory tool result observed")
            return h.completion_accept()
        if route == "goal_completion_review":
            payload = self._payload(messages)
            criteria = payload["mission"]["success_criteria"]
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "goal review must name every proven origin"
            return h.goal_accept(
                [{"criterion": c, "met": True, "evidence_ids": list(receipts)} for c in criteria]
            )
        if route == "next_action":
            self._actions_calls += 1
            node_id = await self._host_node_id()
            if self._actions_calls == 1:
                return h.turn_actions(node_id)
            payload = self._payload(messages)
            run_id = payload["active_path"]["run_id"]
            records = await self._evidence.list_for_run(run_id)
            tool_evidence = [
                str(r.evidence_id)
                for r in records
                if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            if self._actions_calls == 2:
                assert tool_evidence, "turn 2 needs the executed call's evidence"
                step_id = payload["active_path"]["steps"][0]["step_id"]
                return {**h.turn_complete_step(tool_evidence), "path_step_id": step_id}
            if payload.get("active_task"):
                # Follow stored task state if a prior turn exhausted its decision budget.
                # Explicit evidence selection: keep the executed call's evidence on
                # the proposal with the required outcome so the explicit-evidence
                # consumption path is exercised.
                assert tool_evidence, "turn 3 needs the executed call's evidence"
                return {
                    "mode": "complete_task",
                    "reason": "task evidence ready",
                    "actions": [],
                    "path": None,
                    "path_step_id": None,
                    "completion": {
                        "outcome": "success",
                        "reason": "inventory evidence ready",
                        "evidence_ids": list(tool_evidence),
                    },
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            return h.turn_complete_goal()
        raise AssertionError(f"no live answer for route {route!r}")


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
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=8),
    )


async def test_live_run_reaches_step_and_mission_completion_with_one_call():
    """Real loop: 1 counted execution, verify/update, step + mission close."""
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
    path = await deps.path_store.get_active(str(runtime.run_id))
    assert path is not None
    assert any(str(s.status) == "completed" and s.evidence_ids for s in path.steps)
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    assert any(isinstance(r.metadata.get("tool_result"), dict) for r in records)


async def test_live_run_with_single_allowed_call_closes_the_goal():
    """max_actions=1: one real call, then non-executing review closes the goal."""
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
    path = await deps.path_store.get_active(str(runtime.run_id))
    assert path is not None
    assert any(str(s.status) == "completed" and s.evidence_ids for s in path.steps)
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    assert any(isinstance(r.metadata.get("tool_result"), dict) for r in records)
