"""Production loop and event chain, with explicit model and gateway test doubles."""

from __future__ import annotations

import json
from typing import Any

import pytest

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, MissionStatus, Verdict
from redteam.core.ids import GraphNodeId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import REPLAN_TRIGGERED, REPORT_PROJECTED
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
from redteam.orchestration.work.models import WorkStatus
from tests.integration import _engine_llm_harness as h
from tests.integration.test_engine_live_run_flow import CountingGateway


def _native_tool_registry() -> LocalToolRegistry:
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return registry


def _executed_evidence_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Stored evidence behind executed tool messages, never raw payloads."""
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


class NativeE2ELLM:
    """One inventory call, then task and goal settlement through local tools.

    Only ``complete_native`` drives the loop. ``complete_structured`` answers
    the genuine settlement reviews (verification, task settlement, goal
    settlement); any legacy strategy route raises instead of standing in.
    """

    def __init__(self) -> None:
        self.turns = 0
        self.reviews: list[str] = []
        self._task_call: str | None = None
        self._goal_call: str | None = None

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = {str(getattr(spec, "name", "")) for spec in tools}
        inventory = exposed_tool_name(h.INVENTORY_TOOL)
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert inventory in names, "native model needs the registered inventory tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        evidenced = _executed_evidence_ids(messages)
        if self.turns == 1:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(call_id="call-1", name=inventory, arguments={}),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-task"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "task evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="holding for settlement")
        if self._goal_call is None:
            self._goal_call = "call-goal"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "goal evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        props = response_schema.get("properties", {})
        if "claim_supported" in props:
            route = "analysis_verify"
        elif "claims" in props:
            route = "task_completion_review"
        elif "criteria" in props:
            route = "goal_completion_review"
        else:
            raise AssertionError("legacy strategy route is not part of the native loop")
        self.reviews.append(route)
        if route == "analysis_verify":
            return h.meaning("success", True)
        payload = json.loads(messages[0]["content"])
        receipts = list(payload["verified_source_ids"])
        assert receipts, "settlement review needs the verified executed call evidence"
        if route == "task_completion_review":
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
        criteria = payload["mission"]["success_criteria"]
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

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


async def test_minimal_vertical_slice_end_to_end(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    engagement, deps, gateway = _world()
    events: list[EventEnvelope] = []

    async def collect(event: EventEnvelope) -> None:
        events.append(event)

    await deps.event_bus.subscribe("*", collect)
    final = await run_engagement(engagement, deps)
    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.GOAL_ACHIEVED
    assert final.last_verdict is Verdict.UNKNOWN
    assert final.actions_executed == len(gateway.calls) == 1
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None and mission.status is MissionStatus.COMPLETED
    llm = deps.llm_client
    assert isinstance(llm, NativeE2ELLM)
    assert {"task_completion_review", "goal_completion_review"} <= set(llm.reviews)
    types = [event.event_type for event in events]
    required_order = [
        EventType.RUN_STARTED,
        EventType.MISSION_CREATED,
        EventType.TASK_CREATED,
        EventType.TASK_STARTED,
        EventType.AGENT_STARTED,
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
        EventType.ACTION_APPROVED,
        EventType.TOOL_EXECUTION_STARTED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.EVIDENCE_CREATED,
        "ObservationRecorded",
        EventType.GRAPH_UPDATED,
        EventType.TASK_COMPLETED,
        EventType.MISSION_UPDATED,
        REPLAN_TRIGGERED,
        REPORT_PROJECTED,
        EventType.RUN_FINISHED,
    ]
    cursor = 0
    for kind in required_order:
        cursor = types.index(kind, cursor) + 1
    assert all(event.run_id == final.run_id for event in events)
    assert all(event.engagement_id == engagement.engagement_id for event in events)
    # Settlement events come from the semantic owner: the accepted task
    # completion announces its canonical task, and the accepted goal
    # settlement announces the completed mission, each citing its cause.
    seen = {event.event_id for event in events}
    task_completed = next(
        event for event in events if event.event_type == EventType.TASK_COMPLETED
    )
    mission_updated = next(
        event for event in events if event.event_type == EventType.MISSION_UPDATED
    )
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    assert task_completed.payload["task_id"] in {task.task_id for task in tasks}
    assert task_completed.payload["mission_id"] == final.mission_id
    assert mission_updated.payload == {"mission_id": final.mission_id, "status": "completed"}
    for event in (task_completed, mission_updated):
        assert event.correlation_id == str(final.run_id)
        assert event.causation_id is not None and event.causation_id in seen
        assert all(
            earlier.event_id != event.causation_id for earlier in events[events.index(event):]
        )
    assert events.index(task_completed) < events.index(mission_updated)
    proposal = next(event for event in events if event.event_type == EventType.ACTION_PROPOSED)
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    raw = [record for record in evidence if isinstance(record.metadata.get("tool_result"), dict)]
    assert len(raw) == 1 and raw[0].action_id == proposal.payload["action_id"]
    assert raw[0].invocation_id == gateway.calls[0].invocation_id
    # The native loop fabricates no investigation path: the executed call is
    # admitted to the work ledger instead, with the stored tool evidence behind it.
    assert await deps.path_store.list_paths(str(final.run_id)) == []
    work = await deps.work_store.list_items(run_id=final.run_id)
    assert any(item.status is WorkStatus.SUCCEEDED for item in work)
    node = await deps.graph_store.get_node(GraphNodeId(f"host:{h.HOST}"))
    assert node is not None
    # Direct-tool runs record the actually invoked tool id, not the legacy
    # capability.operation key: prove the ledger entry names the executed call.
    assert str(gateway.calls[0].tool_id) in facts_of(node.properties)["attempted_operations"]
    assert (tmp_path / "data" / "reports" / f"{final.run_id}.json").exists()


async def test_quick_start_objective_reaches_mission_without_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    engagement, deps, gateway = _world()
    final = await run_engagement(
        engagement,
        deps,
        objective="VulnBank \uc6f9 \ub808\ub4dc\ud305\ubc0d, "
        "\uc778\uc99d/\uc778\uac00 \uc911\uc2ec. token=privatevalue",
    )
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None
    assert mission.priorities[:2] == ["authentication", "authorization"]
    assert "privatevalue" not in mission.model_dump_json()
    assert len(gateway.calls) == 1
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    assert len(tasks) == 1 and tasks[0].objective == mission.objective


async def test_bare_composition_requires_explicit_llm(tmp_path) -> None:
    with pytest.raises(TypeError, match="llm"):
        await build_runtime_deps(_engagement(), evidence_dir=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="LLM"):
        await build_runtime_deps(_engagement(), evidence_dir=tmp_path, llm=None)  # type: ignore[arg-type]


def _engagement():
    return h.make_engagement(engagement_id="eng_e2e")


def _world():
    engagement = _engagement()
    llm = NativeE2ELLM()
    gateway = CountingGateway()
    catalog = h.FakeCatalog([h.inventory_descriptor()])
    deps = RuntimeDeps(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement},
            outbound_descriptor_resolver=catalog.get,
        ),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=catalog,
        local_tools=_native_tool_registry(),
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=8),
    )
    return engagement, deps, gateway
