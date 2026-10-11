"""Live task-completion boundary: required tasks, optional guard, receipts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import (
    AgentPhase,
    MissionStatus,
    RiskLevel,
    TaskStatus,
    ToolHealthStatus,
)
from redteam.core.ids import (
    EngagementId,
    MissionId,
    TaskId,
    ToolId,
    new_id,
)
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
from redteam.orchestration.paths import CompletionCriterion, InMemoryPathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkStatus
from tests.integration import _engine_llm_harness as h

SECOND_HOST = "a2.local"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _two_host_engagement(engagement_id: str) -> Engagement:
    hosts = [h.HOST, SECOND_HOST]
    return Engagement(
        engagement_id=EngagementId(engagement_id),
        name="engine task boundary",
        scope=EngagementScope(allowed_hosts=hosts),
        rules_of_engagement=[],
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(
            max_actions_per_minute=30,
            max_concurrent_actions=2,
        ),
        approval_policy=ApprovalPolicy(),
        created_at=_utcnow(),
        metadata={"target_refs": {host: host for host in hosts}},
    )


def _web_read_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="test-harness",
        tool_name="http_request",
        description="neutral web read for scope test",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "method": {"type": "string"},
            },
            "additionalProperties": True,
        },
        operations=["http_request"],
        capability_tags=["web"],
        target_types=[],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.UNKNOWN,
    )


class CountingGateway:
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
            output={"inventory": "ok"},
            started_at=now,
            finished_at=now,
            metadata={},
        )


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


def _settled_ok(messages: list[dict[str, Any]], call_id: str) -> bool:
    seen = _tool_result(messages, call_id)
    return isinstance(seen, dict) and seen.get("ok") is True


def _executed_ok(messages: list[dict[str, Any]], call_id: str) -> bool:
    seen = _tool_result(messages, call_id)
    return (
        isinstance(seen, dict) and seen.get("ok") is True and seen.get("executed") is True
    )


class TaskBoundaryLLM(h.ScriptedLLM):
    """Native probe/settle script: inventory per host, settle per task, then the goal.

    Only ``complete_native`` drives the loop. ``complete_structured`` serves
    queued engine-direct answers first (tests driving InvestigationEngine
    straight), then the genuine settlement reviews; any legacy strategy
    route raises instead of standing in for a native turn.
    """

    def __init__(
        self,
        *,
        graph_store: Any,
        evidence_store: Any,
        probes: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._graph = graph_store
        self._evidence = evidence_store
        self._probes = list(probes) if probes is not None else [h.HOST, SECOND_HOST]
        self.turns = 0
        self.reviews: list[str] = []
        self.task_evidence: list[list[str]] = []
        self._probe_calls: list[str] = []
        self._settle_calls: list[str] = []
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
        for index, host in enumerate(self._probes):
            if index >= len(self._probe_calls):
                call_id = f"call-probe-{index + 1}"
                self._probe_calls.append(call_id)
                return NativeCompletion(
                    tool_calls=(
                        NativeToolCall(
                            call_id=call_id, name=inventory, arguments={"host": host}
                        ),
                    ),
                    text="",
                )
            if not _executed_ok(messages, self._probe_calls[index]):
                return NativeCompletion(tool_calls=(), text="holding for execution")
            if index >= len(self._settle_calls):
                evidenced = _executed_evidence_ids(messages)
                prior = {known for settled in self.task_evidence for known in settled}
                fresh = [item for item in evidenced if item not in prior]
                assert fresh, "each task settlement needs its own executed evidence"
                call_id = f"call-task-{index + 1}"
                self._settle_calls.append(call_id)
                self.task_evidence.append(list(fresh))
                return NativeCompletion(
                    tool_calls=(
                        NativeToolCall(
                            call_id=call_id,
                            name=task_tool,
                            arguments={
                                "outcome": "success",
                                "reason": "task evidence ready",
                                "evidence_ids": list(fresh),
                            },
                        ),
                    ),
                    text="",
                )
            if not _settled_ok(messages, self._settle_calls[index]):
                return NativeCompletion(tool_calls=(), text="holding for settlement")
        if self._goal_call is None:
            evidenced = _executed_evidence_ids(messages)
            assert evidenced, "goal settlement needs the executed call evidence"
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
        messages: Any,
        response_schema: Any,
        system: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        queue = self._routes.get(route, [])
        if queue:
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        props = response_schema.get("properties", {})
        if "claim_supported" in props:
            review = "analysis_verify"
        elif "criteria" in props:
            review = "goal_completion_review"
        elif "claims" in props or "accepted" in props:
            review = "task_completion_review"
        else:
            raise AssertionError("legacy strategy route is not part of the native loop")
        self.reviews.append(review)
        if review == "analysis_verify":
            return h.meaning("success", True)
        payload = json.loads(messages[0]["content"])
        receipts = list(payload["verified_source_ids"])
        assert receipts, "settlement review needs the verified executed call evidence"
        if review == "task_completion_review":
            return {
                "accepted": True,
                "reason": "task review accept",
                "claims": [
                    {
                        "description": "actual result observed in stored evidence",
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
            "reason": "goal review accept",
            "criteria": [
                {
                    "criterion": item,
                    "met": True,
                    "evidence_ids": list(receipts),
                    "claims": [
                        {
                            "description": "actual result observed in stored evidence",
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

    async def complete(self, *, messages: Any, system: Any = None, **_: Any) -> str:
        return ""


def _live_deps(
    llm: TaskBoundaryLLM,
    gateway: CountingGateway,
    engagement: Engagement,
    event_bus: InMemoryEventBus,
    task_store: InMemoryTaskGraphStore,
) -> RuntimeDeps:
    catalog = h.FakeCatalog([h.inventory_descriptor()])
    return RuntimeDeps(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement},
            outbound_descriptor_resolver=catalog.get,
        ),
        tool_gateway=gateway,
        event_bus=event_bus,
        graph_store=llm._graph,
        evidence_store=llm._evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=catalog,
        local_tools=_native_tool_registry(),
        work_store=InMemoryWorkStore(),
        task_graph_store=task_store,
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=20, decision_max_attempts=6),
    )


async def _install_dependent_second_task(
    event_bus: InMemoryEventBus,
    task_store: InMemoryTaskGraphStore,
    seen: dict[str, Any],
) -> None:
    async def _on_mission_created(event: Any) -> None:
        if seen.get("done"):
            return
        mission_id = event.payload.get("mission_id")
        if not mission_id:
            return
        tasks = await task_store.list_tasks(mission_id=mission_id)
        if len(tasks) != 1:
            return
        first = tasks[0]
        now = _utcnow()
        second = MissionTask(
            task_id=TaskId(new_id("task")),
            mission_id=first.mission_id,
            title="Second required task on second host",
            objective="map the second in-scope host a2.local",
            status=TaskStatus.PENDING,
            required=True,
            dependency_task_ids=[first.task_id],
            provenance=Provenance(source="test:hook"),
            created_at=now,
            updated_at=now,
        )
        await task_store.create_task(second)
        seen["done"] = True
        seen["first_id"] = str(first.task_id)
        seen["second_id"] = str(second.task_id)
        seen["second_initial"] = "pending"

    await event_bus.subscribe(EventType.MISSION_CREATED, _on_mission_created)


async def _install_optional_pending_task(
    event_bus: InMemoryEventBus,
    task_store: InMemoryTaskGraphStore,
    seen: dict[str, Any],
) -> None:
    async def _on_mission_created(event: Any) -> None:
        if seen.get("done"):
            return
        mission_id = event.payload.get("mission_id")
        if not mission_id:
            return
        tasks = await task_store.list_tasks(mission_id=mission_id)
        if len(tasks) != 1:
            return
        first = tasks[0]
        now = _utcnow()
        optional = MissionTask(
            task_id=TaskId(new_id("task")),
            mission_id=first.mission_id,
            title="Optional follow-up",
            objective=first.objective,
            status=TaskStatus.PENDING,
            required=False,
            provenance=Provenance(source="test:optional"),
            created_at=now,
            updated_at=now,
        )
        await task_store.create_task(optional)
        seen["done"] = True
        seen["required_id"] = str(first.task_id)
        seen["optional_id"] = str(optional.task_id)

    await event_bus.subscribe(EventType.MISSION_CREATED, _on_mission_created)


async def test_two_required_tasks_dependency_promotion_closes_goal() -> None:
    """Dependent PENDING promotes to READY, loop closes goal."""
    llm = TaskBoundaryLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
        probes=[h.HOST, SECOND_HOST],
    )
    gateway = CountingGateway()
    engagement = _two_host_engagement("eng-task-boundary")
    event_bus = InMemoryEventBus()
    task_store = InMemoryTaskGraphStore()
    seen: dict[str, Any] = {}
    await _install_dependent_second_task(event_bus, task_store, seen)
    deps = _live_deps(llm, gateway, engagement, event_bus, task_store)
    runtime = await run_engagement(engagement, deps)
    assert seen.get("done"), "hook must create second task"
    assert seen.get("second_initial") == "pending"
    assert len(gateway.calls) == 2, f"expected 2 tool calls, got {len(gateway.calls)}"
    assert [str(call.tool_id) for call in gateway.calls] == [h.INVENTORY_TOOL] * 2
    assert gateway.calls[0].arguments.get("host") == h.HOST
    assert gateway.calls[1].arguments.get("host") == SECOND_HOST
    assert llm.reviews.count("task_completion_review") == 2, "each task settles reviewed"
    assert "goal_completion_review" in llm.reviews
    assert runtime.mission_id is not None
    mission = await deps.task_graph_store.get_mission(runtime.mission_id)
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED
    assert runtime.termination_reason == TerminationReason.GOAL_ACHIEVED
    tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
    by_id = {str(t.task_id): t for t in tasks}
    first = by_id[seen["first_id"]]
    second = by_id[seen["second_id"]]
    assert first.status == TaskStatus.COMPLETED
    assert second.status == TaskStatus.COMPLETED
    started = event_bus.history(event_type=EventType.TASK_STARTED)
    started_ids = [str(e.payload.get("task_id")) for e in started]
    assert seen["first_id"] in started_ids
    assert seen["second_id"] in started_ids
    assert started_ids.index(seen["first_id"]) < started_ids.index(seen["second_id"])
    # The native loop emits no task-completion events and fabricates no
    # investigation paths: settlement is proven through the work ledger and
    # the stored tool evidence each settlement cited.
    assert await deps.path_store.list_paths(str(runtime.run_id)) == []
    assert len(llm.task_evidence) == 2
    assert all(llm.task_evidence)
    assert set(llm.task_evidence[0]).isdisjoint(llm.task_evidence[1])
    items = await deps.work_store.list_items(run_id=runtime.run_id)
    first_work = [item for item in items if item.model_tool_call_id == "call-probe-1"]
    second_work = [item for item in items if item.model_tool_call_id == "call-probe-2"]
    assert len(first_work) == 1 and first_work[0].status is WorkStatus.SUCCEEDED
    assert len(second_work) == 1 and second_work[0].status is WorkStatus.SUCCEEDED
    assert first_work[0].mission_task_id == seen["first_id"]
    assert second_work[0].mission_task_id == seen["second_id"]
    tool_ids = {
        str(record.evidence_id)
        for record in await deps.evidence_store.list_for_run(runtime.run_id)
        if isinstance(getattr(record, "metadata", {}).get("tool_result"), dict)
    }
    assert tool_ids, "goal needs the executed call evidence"
    assert set(llm.task_evidence[0]) <= tool_ids
    assert set(llm.task_evidence[1]) <= tool_ids
    assert mission.criterion_results, "goal settlement keeps its evidence results"
    for result in mission.criterion_results:
        assert result.met and result.evidence_ids
        assert set(map(str, result.evidence_ids)) <= tool_ids


async def test_optional_pending_task_never_emits_false_completion() -> None:
    llm = TaskBoundaryLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
        probes=[h.HOST],
    )
    gateway = CountingGateway()
    engagement = h.make_engagement(engagement_id="eng-optional")
    event_bus = InMemoryEventBus()
    task_store = InMemoryTaskGraphStore()
    seen: dict[str, Any] = {}
    await _install_optional_pending_task(event_bus, task_store, seen)
    deps = _live_deps(llm, gateway, engagement, event_bus, task_store)
    runtime = await run_engagement(engagement, deps)
    assert seen.get("done")
    assert runtime.mission_id is not None
    mission = await deps.task_graph_store.get_mission(runtime.mission_id)
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED
    required = await deps.task_graph_store.get_task(TaskId(seen["required_id"]))
    assert required is not None and required.status is TaskStatus.COMPLETED
    optional = await deps.task_graph_store.get_task(TaskId(seen["optional_id"]))
    assert optional is not None
    assert optional.status is not TaskStatus.COMPLETED
    started = event_bus.history(event_type=EventType.TASK_STARTED)
    started_ids = [str(e.payload.get("task_id")) for e in started]
    assert seen["required_id"] in started_ids
    assert seen["optional_id"] not in started_ids
    assert len(gateway.calls) == 1


async def test_wrong_task_receipts_rejected() -> None:
    """Cross-task and cross-run receipts never close a task."""
    from redteam.attack_graph.service import AttackGraphService
    from redteam.orchestration.completion import validate_completion
    from redteam.orchestration.engine import (
        InvestigationEngine,
        TurnDecision,
    )
    from redteam.orchestration.paths import PathPlan, PathStep

    llm = TaskBoundaryLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
    )
    world = h.make_world(llm)
    seeded = await h.seed_host("run-receipt-guard", h.HOST)
    world.graph_store = seeded.store
    world.deps.graph_store = seeded.store
    world.deps.engagement = h.make_engagement()
    engine = InvestigationEngine(world.deps)
    runtime = h.make_runtime("run-receipt-guard", "eng-t1", mission_id="m-guard")
    runtime = runtime.model_copy(update={"active_task_id": TaskId("task-guard")})
    now = _utcnow()
    mission_id = MissionId("m-guard")
    from redteam.core.contracts.mission import MissionPlan

    await world.tasks.create_mission(
        MissionPlan(
            mission_id=mission_id,
            engagement_id=EngagementId("eng-t1"),
            objective="guard",
            status=MissionStatus.ACTIVE,
            success_criteria=["guard"],
            created_at=now,
            updated_at=now,
            provenance=Provenance(source="test:guard"),
        )
    )
    await world.tasks.create_task(
        MissionTask(
            task_id=TaskId("task-guard"),
            mission_id=mission_id,
            title="guard task",
            objective="guard",
            status=TaskStatus.RUNNING,
            required=True,
            provenance=Provenance(source="test:guard"),
            created_at=now,
            updated_at=now,
        )
    )
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(world.tasks, world.deps.path_store).bind(
        str(runtime.run_id), str(mission_id), str(runtime.engagement_id)
    )
    other_path = PathPlan(
        path_id="path-other",
        run_id="run-receipt-guard",
        mission_id="m-guard",
        mission_task_id="task-other",
        version=1,
        status="active",
        reason="other",
        goal="guard",
        steps=[
            PathStep(
                step_id="step-1",
                objective="x",
                agent_id=h.RECON_AGENT,
                target_node_ids=[seeded.node_id],
                allowed_operations=[h.CANONICAL_OP],
                completion_criteria=[
                    CompletionCriterion(kind="evidence", description="x", min_evidence=1)
                ],
                required=True,
                status="completed",
                outcome="success",
                evidence_ids=["ev-other"],
            )
        ],
    )
    ctx: dict[str, Any] = {
        "knowledge": {
            "known_targets": [seeded.node_id],
            "facts": [],
            "fact_refs": [],
        }
    }
    errs = await engine.complete_task(
        other_path,
        TurnDecision(mode="complete_task", reason="x"),
        runtime,
        ctx,
    )
    assert errs, "unbound task path must be rejected"
    assert any("bound" in e.lower() for e in errs)
    foreign = h.make_tool_evidence(
        evidence_id="ev-foreign",
        run_id="run-other",
        engagement_id="eng-t1",
        action_id="action-foreign",
        invocation_id="inv-foreign",
        path_id="path-other",
        path_version=1,
        step_id="step-1",
        target_node_id=seeded.node_id,
    )
    await world.evidence.append(foreign)
    own_path = PathPlan(
        path_id="path-own",
        run_id="run-receipt-guard",
        mission_id="m-guard",
        version=1,
        status="active",
        reason="own",
        goal="guard",
        steps=[
            PathStep(
                step_id="step-1",
                objective="x",
                agent_id=h.RECON_AGENT,
                target_node_ids=[seeded.node_id],
                allowed_operations=[h.CANONICAL_OP],
                completion_criteria=[
                    CompletionCriterion(kind="evidence", description="x", min_evidence=1)
                ],
                required=True,
                status="pending",
            )
        ],
    )
    errs2 = await validate_completion(
        path=own_path,
        step=own_path.steps[0],
        proposal={
            "evidence_ids": ["ev-foreign"],
            "outcome": "success",
            "reason": "x",
        },
        evidence_store=world.evidence,
        graph_service=AttackGraphService(world.graph_store),
        run_id="run-receipt-guard",
    )
    assert errs2, "cross-run receipt must be rejected"


async def test_authorization_exclusion_allows_independent_web_read() -> None:
    """Excluded authz blocks authz step, not neutral read."""
    from redteam.attack_graph.identity import endpoint_node_id
    from redteam.attack_graph.predicates import Confidence
    from redteam.attack_graph.predicates import Provenance as GraphProv
    from redteam.attack_graph.service import AttackGraphService
    from redteam.core.contracts.mission import MissionPlan
    from redteam.core.contracts.target import EndpointTarget
    from redteam.core.enums import GraphNodeType
    from redteam.core.ids import RunId, ToolId
    from redteam.orchestration.agents.base import PlanStep
    from redteam.orchestration.engine import (
        ActionIntent,
        InvestigationEngine,
        validate_current_action,
    )
    from redteam.orchestration.paths import (
        CompletionCriterion,
        PathPlan,
        PathStep,
    )
    from redteam.orchestration.state import RuntimeState
    from tests.fakes import build_test_application

    deps = build_test_application(
        tool_catalog=h.FakeCatalog([_web_read_descriptor()]),
    )
    mission_id = MissionId("mission-authz-scope")
    now = _utcnow()
    await deps.task_graph_store.create_mission(
        MissionPlan(
            mission_id=mission_id,
            engagement_id=EngagementId("eng_authz_scope"),
            objective="Assess endpoints",
            status=MissionStatus.ACTIVE,
            operator_preferences={"excluded_areas": ["authorization"]},
            provenance=Provenance(source="test"),
            created_at=now,
            updated_at=now,
        )
    )
    run_id = RunId("run-authz-scope")
    plain_id = endpoint_node_id("GET", "/api/health", host="a.local")
    authz_id = endpoint_node_id("GET", "/api/orders/1234", host="a.local")
    graph = AttackGraphService(deps.graph_store)
    for nid, endpoint_path in (
        (plain_id, "/api/health"),
        (authz_id, "/api/orders/1234"),
    ):
        await graph.upsert_node(
            GraphNodeType.ENDPOINT,
            node_id=nid,
            attrs={"in_scope": True},
            facts={"host": "a.local", "path": endpoint_path, "method": "GET"},
            provenance=GraphProv(
                step=0,
                source="engagement:eng_authz_scope",
                confidence=Confidence.SERVER_DECLARED,
                run_id=str(run_id),
            ),
        )
    runtime = RuntimeState(
        run_id=run_id,
        engagement_id=EngagementId("eng_authz_scope"),
        mission_id=mission_id,
        phase=AgentPhase.PLAN,
    )
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(deps.task_graph_store, deps.path_store).bind(
        str(run_id), str(mission_id), str(runtime.engagement_id)
    )
    path = PathPlan(
        path_id="path-authz-scope",
        run_id=str(run_id),
        mission_id=str(mission_id),
        version=1,
        status="active",
        goal="Assess endpoints",
        reason="scope check",
        steps=[
            PathStep(
                step_id="read",
                objective="Inventory public health headers",
                target_node_ids=[str(plain_id)],
                allowed_operations=["web.http_request"],
                completion_criteria=[CompletionCriterion(kind="evidence")],
            ),
            PathStep(
                step_id="authz",
                objective="Verify authorization on orders",
                target_node_ids=[str(authz_id)],
                allowed_operations=["web.http_request"],
                completion_criteria=[CompletionCriterion(kind="evidence")],
            ),
        ],
    )
    await deps.path_store.register(path, expected_version=None)
    engine = InvestigationEngine(deps)
    context = await engine.context(runtime, await graph.observe(step=1))
    neutral = ActionIntent(
        path_step_id="read",
        agent_id="web-api-agent",
        target_node_id=str(plain_id),
        capability="web",
        operation="http_request",
        tool_id="web.http_request",
        arguments={"path": "/api/health", "method": "GET"},
        reason="read public headers",
    )
    blocked = ActionIntent(
        path_step_id="authz",
        agent_id="web-api-agent",
        target_node_id=str(authz_id),
        capability="web",
        operation="http_request",
        tool_id="web.http_request",
        arguments={"path": "/api/orders/1234", "method": "GET"},
        reason="check object authorization",
    )
    compiled_ok, errors_ok = await engine.compile_actions(path, [neutral], runtime, context)
    assert errors_ok == [], f"neutral read blocked: {errors_ok}"
    assert len(compiled_ok) == 1
    compiled_bad, errors_bad = await engine.compile_actions(path, [blocked], runtime, context)
    assert compiled_bad == []
    assert any("operator_excluded_area" in e for e in errors_bad)
    ok_step = PlanStep(
        step_id="read",
        path_id=path.path_id,
        path_version=path.version,
        path_step_id="read",
        capability="web",
        operation="http_request",
        tool_id=ToolId("web.http_request"),
        target=EndpointTarget(
            host="a.local",
            path="/api/health",
            graph_node_id=plain_id,
        ),
        arguments={"path": "/api/health", "method": "GET"},
        reason="read public headers",
    )
    bad_step = PlanStep(
        step_id="authz",
        path_id=path.path_id,
        path_version=path.version,
        path_step_id="authz",
        capability="web",
        operation="http_request",
        tool_id=ToolId("web.http_request"),
        target=EndpointTarget(
            host="a.local",
            path="/api/orders/1234",
            graph_node_id=authz_id,
        ),
        arguments={"path": "/api/orders/1234", "method": "GET"},
        reason="check object authorization",
    )
    assert await validate_current_action(deps, runtime, ok_step) == []
    assert "operator_excluded_area" in await validate_current_action(deps, runtime, bad_step)


async def test_pending_future_required_step_still_allows_task_then_goal() -> None:
    from redteam.attack_graph.observation import GraphObservation
    from redteam.core.contracts.mission import MissionPlan
    from redteam.orchestration.coordination import coordinator_for
    from redteam.orchestration.engine import InvestigationEngine, TurnDecision
    from redteam.orchestration.paths import PathPlan, PathStep

    run_id = "run-future-step"
    llm: Any = h.ScriptedLLM()
    world = h.make_world(llm)
    seeded = await h.seed_host(run_id, h.HOST)
    world.graph_store = seeded.store
    world.deps.graph_store = seeded.store
    world.deps.engagement = h.make_engagement()
    runtime = h.make_runtime(run_id, "eng-t1", mission_id="m-future", task_id="task-future")
    now = _utcnow()
    mission = MissionPlan(
        mission_id=MissionId("m-future"),
        engagement_id=EngagementId("eng-t1"),
        objective="map the in-scope host",
        status=MissionStatus.ACTIVE,
        success_criteria=["map the host"],
        created_at=now,
        updated_at=now,
        provenance=Provenance(source="test:future-step"),
    )
    task = MissionTask(
        task_id=TaskId("task-future"),
        mission_id=MissionId("m-future"),
        title="map the host",
        objective="map the in-scope host",
        status=TaskStatus.RUNNING,
        required=True,
        provenance=Provenance(source="test:future-step"),
        created_at=now,
        updated_at=now,
    )
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    await coordinator_for(world.tasks, world.deps.path_store).bind(run_id, "m-future", "eng-t1")
    ev_id = "ev-future-ready"
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id=ev_id,
            run_id=run_id,
            engagement_id="eng-t1",
            action_id="action-future-ready",
            invocation_id="inv-future-ready",
            path_id="path-future",
            path_version=1,
            step_id="step-1",
            target_node_id=seeded.node_id,
        )
    )
    path = PathPlan(
        path_id="path-future",
        run_id=run_id,
        mission_id="m-future",
        mission_task_id="task-future",
        version=1,
        status="active",
        reason="future step stays pending",
        goal="map the in-scope host",
        steps=[
            PathStep(
                step_id="step-1",
                objective="confirm host answers inventory",
                agent_id=h.RECON_AGENT,
                target_node_ids=[seeded.node_id],
                allowed_operations=[h.CANONICAL_OP],
                completion_criteria=[
                    CompletionCriterion(kind="evidence", description="inventory observed")
                ],
                required=True,
                status="completed",
                evidence_ids=[ev_id],
            ),
            PathStep(
                step_id="step-future",
                objective="follow-up sweep not yet executed",
                agent_id=h.RECON_AGENT,
                target_node_ids=[seeded.node_id],
                allowed_operations=[h.CANONICAL_OP],
                completion_criteria=[
                    CompletionCriterion(kind="evidence", description="future sweep")
                ],
                required=True,
                status="pending",
            ),
        ],
    )
    await world.paths.register(path)
    llm.add("completion_review", h.task_accept([ev_id], reason="task purpose proven"))
    llm.add(
        "goal_completion_review",
        h.goal_accept([{"criterion": "map the host", "met": True, "evidence_ids": [ev_id]}]),
    )
    engine = InvestigationEngine(world.deps)
    task_errors = await engine.complete_task(
        path,
        TurnDecision(
            mode="complete_task",
            reason="task evidence ready",
            completion={"evidence_ids": [ev_id], "outcome": "success", "reason": "ready"},
        ),
        runtime,
        await engine.context(runtime, GraphObservation()),
    )
    assert task_errors == []
    done = await world.tasks.get_task(TaskId("task-future"))
    assert done is not None and done.status is TaskStatus.COMPLETED
    assert done.completion_evidence_id is not None
    stored = await world.paths.list_paths(run_id)
    future = next(p for p in stored if p.path_id == "path-future")
    future_step = next(s for s in future.steps if s.step_id == "step-future")
    assert future_step.status != "completed"
    goal_errors = await engine.complete_goal(
        path,
        TurnDecision.model_validate(h.turn_complete_goal()),
        runtime,
        await engine.context(runtime, GraphObservation()),
    )
    assert goal_errors == []
    final_mission = await world.tasks.get_mission(MissionId("m-future"))
    assert final_mission is not None and final_mission.status is MissionStatus.COMPLETED
