"""Event identity checks across the real orchestration vertical slice.

Driven through the production native ReAct loop (model -> tools -> settle)
with an explicit scripted native model: one inventory call, then task and
goal settlement through the registered settle helpers. Event linkage and
call-id propagation are asserted exactly as before; only the model scripting
speaks the genuine native contract (complete_native with NativeCompletion /
NativeToolCall payloads addressed via exposed_tool_name, plus the genuine
completion reviews on complete_structured).
"""

from __future__ import annotations

import json
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import RunId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import HYPOTHESIS_RESOLVED, REPLAN_TRIGGERED, REPORT_PROJECTED
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
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes import DummyToolGateway, FakeEvidenceStore
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


class _NativeScriptBase:
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
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
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
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


class _EventNativeLLM(_NativeScriptBase):
    """Single-host native script: one inventory call, then task and goal settlement."""

    def __init__(self, *, first_arguments: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._first_arguments = dict(first_arguments) if first_arguments else {}
        self._task_call: str | None = None
        self._goal_call: str | None = None

    @staticmethod
    def _wanted(tools: list[Any]) -> tuple[str, str, str]:
        names = _exposed_names(tools)
        inventory = exposed_tool_name(h.INVENTORY_TOOL)
        task_settle = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_settle = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert inventory in names, "native model needs the registered inventory tool"
        assert task_settle in names, "native model needs the task settlement helper"
        assert goal_settle in names, "native model needs the goal settlement helper"
        return inventory, task_settle, goal_settle

    def _record(self, names: list[str]) -> None:
        self.calls.append({"turn": self.turns, "tool_calls": list(names)})

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        inventory, task_tool, goal_tool = self._wanted(tools)
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns == 1:
            self._record([inventory])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-1", name=inventory, arguments=dict(self._first_arguments)
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-task"
            self._record([task_tool])
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
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-goal"
            self._record([goal_tool])
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


def _world():
    engagement = h.make_engagement(engagement_id="eng_e2e")
    llm = _EventNativeLLM(first_arguments={"host": h.HOST})
    gateway = DummyToolGateway(tools=[h.inventory_descriptor()])
    deps = RuntimeDeps(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement},
            outbound_descriptor_resolver=gateway.get,
        ),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=FakeEvidenceStore(),
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        local_tools=_native_tool_registry(),
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=8),
    )
    return engagement, deps, gateway


async def _run_with_events(tmp_path) -> tuple[list[EventEnvelope], RunId]:
    engagement, deps, _gateway = _world()
    events: list[EventEnvelope] = []

    async def collect(event: EventEnvelope) -> None:
        events.append(event)

    await deps.event_bus.subscribe("*", collect)
    final_state = await run_engagement(engagement, deps=deps)
    return events, final_state.run_id


async def test_action_event_chain_keeps_correlation_and_direct_causation(tmp_path) -> None:
    events, _run_id = await _run_with_events(tmp_path)
    action_events = [
        event
        for event in events
        if event.event_type
        in {
            EventType.HYPOTHESIS_CREATED,
            EventType.ACTION_PROPOSED,
            EventType.POLICY_EVALUATED,
            EventType.ACTION_APPROVED,
            EventType.TOOL_EXECUTION_STARTED,
            EventType.TOOL_EXECUTION_FINISHED,
            EventType.EVIDENCE_CREATED,
            "ObservationRecorded",
            HYPOTHESIS_RESOLVED,
            EventType.GRAPH_UPDATED,
        }
    ]

    assert action_events
    correlation_id = action_events[0].correlation_id
    assert correlation_id is not None
    assert all(event.correlation_id == correlation_id for event in action_events)
    for parent, child in zip(action_events, action_events[1:], strict=False):
        assert child.causation_id == parent.event_id


async def test_replan_triggered_keeps_run_identity_and_cites_its_cause(tmp_path) -> None:
    # ReplanTriggered is a run-level decision rather than a step in the action
    # chain, so it is checked on its own terms: the terminal decision must be
    # attributable to the event that produced it, inside the same run.
    events, run_id = await _run_with_events(tmp_path)
    triggered = [
        index for index, event in enumerate(events) if event.event_type == REPLAN_TRIGGERED
    ]

    assert triggered, "a terminal replan must be observable"
    seen = {event.event_id for event in events}
    for index in triggered:
        event = events[index]
        assert event.run_id == run_id
        assert event.engagement_id is not None
        assert event.causation_id is not None
        assert event.causation_id in seen
        assert all(earlier.event_id != event.causation_id for earlier in events[index:]), (
            "the cited cause must not appear after the effect"
        )


async def test_agent_started_cites_run_started_as_causation(tmp_path) -> None:
    events, _run_id = await _run_with_events(tmp_path)
    run_started = next(event for event in events if event.event_type == EventType.RUN_STARTED)
    mission_created = next(
        event for event in events if event.event_type == EventType.MISSION_CREATED
    )
    task_created = [event for event in events if event.event_type == EventType.TASK_CREATED]
    task_started = next(event for event in events if event.event_type == EventType.TASK_STARTED)
    agent_started = next(event for event in events if event.event_type == EventType.AGENT_STARTED)

    assert run_started.correlation_id == str(run_started.run_id)
    assert agent_started.correlation_id == run_started.correlation_id
    # The initial Mission/Task creation chain precedes Task dispatch.
    assert mission_created.causation_id == run_started.event_id
    assert task_created[0].causation_id == mission_created.event_id
    for parent, child in zip(task_created, task_created[1:], strict=False):
        assert child.causation_id == parent.event_id
    assert task_started.causation_id == task_created[-1].event_id
    assert agent_started.causation_id == task_started.event_id


async def test_run_finished_cites_the_last_event_and_keeps_run_correlation(tmp_path) -> None:
    events, run_id = await _run_with_events(tmp_path)
    run_started = next(event for event in events if event.event_type == EventType.RUN_STARTED)
    run_finished = next(event for event in events if event.event_type == EventType.RUN_FINISHED)
    mission_updated = next(
        event for event in events if event.event_type == EventType.MISSION_UPDATED
    )
    report_projected = next(event for event in events if event.event_type == REPORT_PROJECTED)

    assert run_started.correlation_id == str(run_id)
    assert run_finished.correlation_id == str(run_id)
    assert run_finished.causation_id == events[-2].event_id
    report_index = events.index(report_projected)
    assert report_projected.causation_id == events[report_index - 1].event_id
    assert events.index(mission_updated) < report_index


async def test_task_completed_cites_its_cause_and_precedes_mission_update(tmp_path) -> None:
    # The semantic owner announces each accepted task settlement, chained
    # on the review decision that accepted it, ahead of the mission update.
    events, run_id = await _run_with_events(tmp_path)
    task_completed = next(
        event for event in events if event.event_type == EventType.TASK_COMPLETED
    )
    mission_updated = next(
        event for event in events if event.event_type == EventType.MISSION_UPDATED
    )
    report_projected = next(event for event in events if event.event_type == REPORT_PROJECTED)

    assert task_completed.run_id == run_id
    assert task_completed.correlation_id == str(run_id)
    assert task_completed.payload["mission_id"] is not None
    assert task_completed.payload["task_id"] is not None
    seen = {event.event_id for event in events}
    for event in (task_completed, mission_updated):
        assert event.causation_id is not None
        assert event.causation_id in seen
        assert all(
            earlier.event_id != event.causation_id for earlier in events[events.index(event):]
        ), "the cited cause must not appear after the effect"
    assert events.index(task_completed) < events.index(mission_updated)
    assert events.index(mission_updated) < events.index(report_projected)
