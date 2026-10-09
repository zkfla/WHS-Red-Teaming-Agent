"""Driver regressions on ACTUAL native ReAct runners (not fake ordering).

Two real ``AgentLoopRunner`` graphs share one deps bundle and one memory
checkpointer through ``InstanceDriver.drive``: tool intervals overlap while
one instance is in flight (independence), both reach terminal state, and a
two-task mission settles with no duplicate occupancy.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolInvocation, ToolResult
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import (
    AgentInstanceId,
    EngagementId,
    MissionId,
    RunId,
    TaskId,
    new_id,
)
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.checkpointing import build_checkpointer, list_checkpoint_threads
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.instance_driver import InstanceDriver
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
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h


def _engagement(*hosts: str) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng-driver-react"),
        name="driver-react",
        scope=EngagementScope(allowed_hosts=list(hosts)),
        rules_of_engagement=[],
        execution_budget=ExecutionBudget(max_actions=12),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


class _PerConversationLLM:
    """Stateless script: every decision derives from that turn's messages.

    Shared by both runners, so no global turn counter may leak one
    instance's progress into the other's. Reviews accept grounded claims.
    """

    def __init__(self) -> None:
        self.native_turns = 0
        self.turn_times: list[float] = []

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **_: Any,
    ) -> str:
        # Plain-text route of the LLM contract; this script only speaks
        # in tool calls, so a hold marker is the honest answer.
        return "holding: settled"

    @staticmethod
    def _calls_by_id(messages: list[dict[str, Any]]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or not call.get("id"):
                    continue
                # Provider shape: {id, function: {name, arguments}}.
                function = call.get("function") or {}
                name = function.get("name") if isinstance(function, dict) else None
                if name:
                    mapping[str(call["id"])] = str(name)
        return mapping

    @classmethod
    def _executed_evidence(cls, messages: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for message in messages:
            if message.get("role") != "tool" or not message.get("tool_call_id"):
                continue
            try:
                body = json.loads(message.get("content") or "{}")
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            if body.get("ok") is not True or body.get("executed") is not True:
                continue
            for identity in body.get("evidence_ids") or []:
                text = str(identity)
                if text and text not in found:
                    found.append(text)
        return found

    @classmethod
    def _settle_ok(cls, messages: list[dict[str, Any]], name: str) -> bool:
        by_id = cls._calls_by_id(messages)
        for message in messages:
            if message.get("role") != "tool" or not message.get("tool_call_id"):
                continue
            if by_id.get(str(message["tool_call_id"])) != name:
                continue
            try:
                body = json.loads(message.get("content") or "{}")
            except ValueError:
                continue
            if isinstance(body, dict) and body.get("ok") is True:
                return True
        return False

    @staticmethod
    def _has_refusal(messages: list[dict[str, Any]]) -> bool:
        # A refused bind (e.g. same-target call still in flight in the
        # sibling instance) arrives as ok=False. Back off instead of
        # spinning into consecutive-failure death.
        for message in messages:
            if message.get("role") != "tool" or not message.get("tool_call_id"):
                continue
            try:
                body = json.loads(message.get("content") or "{}")
            except ValueError:
                continue
            if isinstance(body, dict) and body.get("ok") is False:
                return True
        return False

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
        assert inventory in names
        assert task_tool in names
        assert goal_tool in names
        self.native_turns += 1
        self.turn_times.append(time.monotonic())
        evidenced = self._executed_evidence(messages)
        if not evidenced:
            if self._has_refusal(messages):
                await asyncio.sleep(0.6)
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=str(new_id("call")),
                        name=inventory,
                        arguments={"host": "a.local"},
                    ),
                ),
                text="",
            )
        if not self._settle_ok(messages, task_tool):
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=str(new_id("call")),
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
        if not self._settle_ok(messages, goal_tool):
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=str(new_id("call")),
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
        return NativeCompletion(tool_calls=(), text="holding: settled")

    async def complete_structured(
        self,
        *,
        messages: Any,
        response_schema: dict[str, Any],
        system: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        props = response_schema.get("properties", {})
        if "claim_supported" in props:
            return h.meaning("success", True)
        payload = json.loads(messages[0]["content"])
        receipts = list(payload["verified_source_ids"])
        assert receipts
        if "claims" in props:
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
                            "description": f"actual result for {item}",
                            "kind": "semantic",
                            "evidence_ids": list(receipts),
                        }
                    ],
                }
                for item in criteria
            ],
        }


class _TimingGateway(DummyToolGateway):
    """Record per-invocation wall intervals, with an optional dwell."""

    def __init__(self, *, dwell: float = 0.0) -> None:
        super().__init__(tools=[h.inventory_descriptor()])
        self._dwell = dwell
        self.intervals: list[tuple[float, float]] = []

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        start = time.monotonic()
        try:
            if self._dwell:
                await asyncio.sleep(self._dwell)
            return await super().invoke(invocation)
        finally:
            self.intervals.append((start, time.monotonic()))


def _react_deps(
    engagement: Engagement,
    gateway: DummyToolGateway,
    evidence_store: FakeEvidenceStore | None = None,
) -> RuntimeDeps:
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return RuntimeDeps(
        control_plane=FakeControlPlane(),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=evidence_store or FakeEvidenceStore(),
        llm_client=_PerConversationLLM(),
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        local_tools=registry,
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(
            max_steps=16, max_consecutive_failures=10, max_no_progress_turns=10
        ),
    )


def _react_deps_sqlite(engagement: Engagement, gateway: DummyToolGateway, path) -> RuntimeDeps:
    from dataclasses import replace

    deps = _react_deps(engagement, gateway)
    return replace(
        deps,
        settings=OrchestrationSettings(
            max_steps=16,
            max_consecutive_failures=10,
            max_no_progress_turns=10,
            checkpointing=True,
            checkpoint_backend="sqlite",
            checkpoint_path=str(path),
        ),
    )


def _runtime(
    run_id: str,
    engagement_id: str,
    instance: str,
    goal: str,
    mission_id: str | None = None,
) -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        mission_id=MissionId(mission_id) if mission_id else None,
        instance_id=AgentInstanceId(instance),
        current_goal=goal,
        remaining_budget=12,
    )


async def _mission_world(deps: RuntimeDeps, task_ids: list[str]) -> str:
    """One ACTIVE mission with the given READY tasks; returns mission id."""
    mission, template = h.make_mission_task(
        mission_id="mission-react", engagement_id="eng-driver-react",
        criteria=["actual response"],
    )
    mission.status = MissionStatus.ACTIVE
    await deps.task_graph_store.create_mission(mission)
    for task_id in task_ids:
        task = template.model_copy(update={"task_id": TaskId(task_id), "title": task_id})
        await deps.task_graph_store.create_task(task)
    return "mission-react"


async def test_two_react_instances_progress_independently_on_shared_checkpoint() -> None:
    engagement = _engagement("a.local")
    gateway = _TimingGateway(dwell=1.0)
    deps = _react_deps(engagement, gateway)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    shared = build_checkpointer(enabled=True, registry=deps.state_codecs)
    goal = "map the in-scope host"
    mission_id = await _mission_world(deps, ["task-r1", "task-r2"])
    async with InstanceDriver(
        deps, settings=deps.settings, checkpointer=shared
    ) as driver:
        events = [
            event
            async for event in driver.drive(
                [
                    _runtime("run-react", str(engagement.engagement_id), "a", goal, mission_id),
                    _runtime("run-react", str(engagement.engagement_id), "b", goal, mission_id),
                ]
            )
        ]
        # Distinct checkpoint threads shared one saver through the driver.
        threads = {
            runner.config()["configurable"]["thread_id"]
            for runner in driver._runners.values()
        }
    assert {event.instance_id for event in events} == {"a", "b"}
    summary = {
        event.instance_id: (event.kind, event.runtime.termination_reason, event.error)
        for event in events
    }
    print("\nEVENT_SUMMARY:", summary)
    assert all(event.kind == "completed" for event in events), summary
    for event in events:
        # Fix repair 1: the event carries its OWN instance runtime.
        assert event.runtime.instance_id == event.instance_id
    assert threads == {"run-react:instance:a", "run-react:instance:b"}
    # Sibling model turns ran while the first tool was still in flight: the
    # instance that lost the same-target race kept judging instead of
    # blocking behind the winner's long job. (Tool intervals themselves do
    # NOT overlap: same-target serialization is existing correct behavior.)
    assert len(gateway.intervals) == 2
    first_start, first_end = gateway.intervals[0]
    assert first_end - first_start >= 1.0
    llm_client = deps.llm_client
    assert isinstance(llm_client, _PerConversationLLM)
    inside = [
        moment
        for moment in llm_client.turn_times
        if first_start < moment < first_end
    ]
    assert len(inside) >= 1
    evidence = await deps.evidence_store.list_for_run(RunId("run-react"))
    tool_results = [
        record
        for record in evidence
        if isinstance(record.metadata.get("tool_result"), dict)
    ]
    assert len(tool_results) == 2
    # Both mission tasks settled exactly once; the shared mission closed.
    for task_id in ("task-r1", "task-r2"):
        stored = await deps.task_graph_store.get_task(TaskId(task_id))
        assert stored is not None
        assert stored.status is TaskStatus.COMPLETED
        assert stored.completion_evidence_id is not None
        assert stored.occupant_owner is None
    mission = await deps.task_graph_store.get_mission(MissionId(mission_id))
    assert mission is not None
    assert mission.status is MissionStatus.COMPLETED


async def test_single_task_settles_once_no_duplicate_occupancy() -> None:
    engagement = _engagement("a.local")
    gateway = _TimingGateway()
    deps = _react_deps(engagement, gateway)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    shared = build_checkpointer(enabled=True, registry=deps.state_codecs)
    goal = "map the in-scope host"
    mission_id = await _mission_world(deps, ["task-only"])
    async with InstanceDriver(
        deps, settings=deps.settings, checkpointer=shared
    ) as driver:
        events = [
            event
            async for event in driver.drive(
                [
                    _runtime("run-solo", str(engagement.engagement_id), "a", goal, mission_id),
                    _runtime("run-solo", str(engagement.engagement_id), "b", goal, mission_id),
                ]
            )
        ]
    assert {event.instance_id for event in events} == {"a", "b"}
    # Race invariants (the loser's end state is timing-dependent by design:
    # a turn after the mission closes ends differently from a turn while it
    # is still active -- both are terminal and neither touches the task).
    summary = {
        event.instance_id: (event.kind, event.runtime.termination_reason, event.error)
        for event in events
    }
    assert all(event.kind in {"completed", "failed"} for event in events), summary
    for event in events:
        assert event.runtime.instance_id == event.instance_id
    # Exactly one settles the shared task; receipts prove single settlement.
    stored = await deps.task_graph_store.get_task(TaskId("task-only"))
    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert stored.completion_evidence_id is not None
    assert stored.occupant_owner is None
    mission = await deps.task_graph_store.get_mission(MissionId(mission_id))
    assert mission is not None
    assert mission.status is MissionStatus.COMPLETED


async def test_occupied_task_holder_keeps_holding_without_seizing() -> None:
    # Deterministic loser path: the task is already RUNNING under another
    # owner, so every observe yields no task and the holder stalls with no
    # active task -- never seizing, never settling another owner's work.
    from datetime import datetime, timezone

    engagement = _engagement("a.local")
    gateway = _TimingGateway()
    deps = _react_deps(engagement, gateway)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    mission_id = await _mission_world(deps, ["task-only"])
    now = datetime.now(timezone.utc)
    stored = await deps.task_graph_store.get_task(TaskId("task-only"))
    assert stored is not None
    await deps.task_graph_store.update_task(
        stored.model_copy(update={"occupant_owner": "run-solo:instance:a", "updated_at": now})
    )
    await deps.task_graph_store.transition_task(TaskId("task-only"), TaskStatus.RUNNING, now=now)
    goal = "map the in-scope host"
    shared = build_checkpointer(enabled=True, registry=deps.state_codecs)
    async with InstanceDriver(deps, settings=deps.settings, checkpointer=shared) as driver:
        events = [
            event
            async for event in driver.drive(
                [_runtime("run-solo", str(engagement.engagement_id), "b", goal, mission_id)]
            )
        ]
    (event,) = events
    assert event.kind == "completed"
    assert event.runtime.instance_id == event.instance_id == "b"
    assert event.runtime.termination_reason == TerminationReason.STALLED
    assert event.runtime.active_task_id is None
    stored = await deps.task_graph_store.get_task(TaskId("task-only"))
    assert stored is not None
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == "run-solo:instance:a"


async def test_sqlite_enumeration_lists_per_instance_threads(tmp_path) -> None:
    # Identity enumeration for full resume: every matching thread counts
    # (idle ones have no work rows), foreign runs are excluded, and no
    # model constructor runs during the pass.
    engagement = _engagement("a.local")
    gateway = _TimingGateway()
    path = tmp_path / "resume.sqlite3"
    deps = _react_deps_sqlite(engagement, gateway, path)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    goal = "map the in-scope host"
    mission_id = await _mission_world(deps, ["task-e1", "task-e2"])
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        events = [
            event
            async for event in driver.drive(
                [
                    _runtime("run-enum", str(engagement.engagement_id), "a", goal, mission_id),
                    _runtime("run-enum", str(engagement.engagement_id), "b", goal, mission_id),
                ]
            )
        ]
    assert {event.instance_id for event in events} == {"a", "b"}
    found = await list_checkpoint_threads(
        backend="sqlite", path=str(path), run_id="run-enum"
    )
    assert {item.instance_id for item in found} == {"a", "b"}
    assert {item.thread_id for item in found} == {
        "run-enum:instance:a",
        "run-enum:instance:b",
    }
    for item in found:
        assert item.run_id == "run-enum"
        assert item.engagement_id == engagement.engagement_id
        assert item.mission_id == mission_id
    assert await list_checkpoint_threads(
        backend="sqlite", path=str(path), run_id="run-other"
    ) == []
