"""Live task-completion boundary: required tasks, optional guard, receipts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import host_node_id
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
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.paths import CompletionCriterion, InMemoryPathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
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


class TaskBoundaryLLM(h.ScriptedLLM):
    """Actions, complete_step, complete_task per task, then goal."""

    def __init__(
        self, *, graph_store: Any, evidence_store: Any
    ) -> None:
        super().__init__()
        self._graph = graph_store
        self._evidence = evidence_store
        self._next_counts: dict[str, int] = {}
        self._task_order: list[str] = []
        self._draft_calls = 0
        self.task_review_payloads: list[dict[str, Any]] = []
        self.step_review_payloads: list[dict[str, Any]] = []

    def _node_for(self, task: Any) -> str:
        if isinstance(task, dict):
            goal = str(task.get("objective", ""))
            if "a2" in goal.lower():
                return str(host_node_id(SECOND_HOST))
        return str(host_node_id(h.HOST))

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    async def complete_structured(
        self,
        *,
        messages: Any,
        response_schema: Any,
        system: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        payload = self._payload(messages)
        self.calls.append(
            {"route": route, "messages": messages, "system": system}
        )
        if route == "path_draft":
            self._draft_calls += 1
            node = self._node_for(payload.get("active_task"))
            draft = h.draft_path(node)
            if self._draft_calls > 1:
                draft["steps"][0]["objective"] = (
                    "confirm the second host answers inventory"
                )
            return draft
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
            if "task" in payload:
                self.task_review_payloads.append(payload)
                return h.completion_accept("task review accept")
            self.step_review_payloads.append(payload)
            return h.completion_accept()
        if route == "goal_completion_review":
            mission = payload["mission"]
            assert isinstance(mission, dict)
            criteria = mission["success_criteria"]
            assert isinstance(criteria, list)
            active = payload["active_path"]
            assert isinstance(active, dict)
            run_id = str(active["run_id"])
            records = await self._evidence.list_for_run(run_id)
            receipts = [
                str(r.evidence_id)
                for r in records
                if isinstance(
                    getattr(r, "metadata", {}).get("tool_result"),
                    dict,
                )
            ]
            return h.goal_accept(
                [
                    {
                        "criterion": c,
                        "met": True,
                        "evidence_ids": list(receipts),
                    }
                    for c in criteria
                ]
            )
        if route == "next_action":
            active_task = payload.get("active_task")
            active_path = payload.get("active_path")
            if not isinstance(active_task, dict):
                return h.turn_complete_goal("close goal")
            assert isinstance(active_path, dict)
            raw_steps = active_path.get("steps")
            assert isinstance(raw_steps, list)
            assert raw_steps and isinstance(raw_steps[0], dict)
            step_id = str(raw_steps[0]["step_id"])
            task_id = str(active_task.get("task_id"))
            if task_id not in self._task_order:
                self._task_order.append(task_id)
            count = self._next_counts.get(task_id, 0) + 1
            self._next_counts[task_id] = count
            node_id = self._node_for(active_task)
            if count == 1:
                turn = h.turn_actions(node_id)
                turn["path_step_id"] = step_id
                for action in turn["actions"]:
                    assert isinstance(action, dict)
                    action["path_step_id"] = step_id
                return turn
            if count == 2:
                path_id = str(active_path.get("path_id"))
                run_id = str(active_path.get("run_id"))
                records = await self._evidence.list_for_run(
                    run_id
                )
                mine = [
                    str(r.evidence_id)
                    for r in records
                    if isinstance(
                        getattr(r, "metadata", {}).get(
                            "tool_result"
                        ),
                        dict,
                    )
                    and str(
                        getattr(r, "metadata", {}).get(
                            "path_id", ""
                        )
                    ) == path_id
                ]
                if not mine:
                    mine = [
                        str(r.evidence_id)
                        for r in records
                        if isinstance(
                            getattr(r, "metadata", {}).get(
                                "tool_result"
                            ),
                            dict,
                        )
                    ]
                assert mine, "turn 2 needs task evidence"
                turn = h.turn_complete_step(mine)
                turn["path_step_id"] = step_id
                return turn
            if count == 3:
                return {
                    "mode": "complete_task",
                    "reason": "task evidence ready",
                    "actions": [],
                    "path": None,
                    "path_step_id": None,
                    "completion": {},
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            return h.turn_complete_goal("task closed")
        raise AssertionError(f"no answer for {route!r}")

    async def complete(
        self, *, messages: Any, system: Any = None, **_: Any
    ) -> str:
        return ""


def _live_deps(
    llm: TaskBoundaryLLM,
    gateway: CountingGateway,
    engagement: Engagement,
    event_bus: InMemoryEventBus,
    task_store: InMemoryTaskGraphStore,
) -> RuntimeDeps:
    return RuntimeDeps(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement}
        ),
        tool_gateway=gateway,
        event_bus=event_bus,
        graph_store=llm._graph,
        evidence_store=llm._evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=h.FakeCatalog([h.inventory_descriptor()]),
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

    await event_bus.subscribe(
        EventType.MISSION_CREATED, _on_mission_created
    )


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

    await event_bus.subscribe(
        EventType.MISSION_CREATED, _on_mission_created
    )


async def test_two_required_tasks_dependency_promotion_closes_goal() -> None:
    """Dependent PENDING promotes to READY, loop closes goal."""
    llm = TaskBoundaryLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
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
    assert len(gateway.calls) == 2, (
        f"expected 2 tool calls, got {len(gateway.calls)}"
    )
    assert llm.task_review_payloads, "task review must run"
    assert llm.step_review_payloads, "step review must run"
    assert runtime.mission_id is not None
    mission = await deps.task_graph_store.get_mission(
        runtime.mission_id
    )
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED
    assert runtime.termination_reason == TerminationReason.GOAL_ACHIEVED
    tasks = await deps.task_graph_store.list_tasks(
        mission_id=runtime.mission_id
    )
    by_id = {str(t.task_id): t for t in tasks}
    first = by_id[seen["first_id"]]
    second = by_id[seen["second_id"]]
    assert first.status == TaskStatus.COMPLETED
    assert second.status == TaskStatus.COMPLETED
    started = event_bus.history(event_type=EventType.TASK_STARTED)
    started_ids = [str(e.payload.get("task_id")) for e in started]
    assert seen["first_id"] in started_ids
    assert seen["second_id"] in started_ids
    completed = event_bus.history(event_type=EventType.TASK_COMPLETED)
    done_ids = [str(e.payload.get("task_id")) for e in completed]
    assert done_ids.count(seen["first_id"]) == 1
    assert done_ids.count(seen["second_id"]) == 1
    order = event_bus.history()
    idx_first = next(
        i
        for i, e in enumerate(order)
        if e.event_type == EventType.TASK_COMPLETED
        and str(e.payload.get("task_id")) == seen["first_id"]
    )
    idx_second = next(
        i
        for i, e in enumerate(order)
        if e.event_type == EventType.TASK_COMPLETED
        and str(e.payload.get("task_id")) == seen["second_id"]
    )
    idx_mission = next(
        i
        for i, e in enumerate(order)
        if e.event_type == EventType.MISSION_UPDATED
    )
    assert idx_first < idx_second < idx_mission
    paths = await deps.path_store.list_paths(str(runtime.run_id))
    task_paths = [p for p in paths if p.mission_task_id]
    assert len({p.mission_task_id for p in task_paths}) >= 2
    first_paths = [
        p for p in task_paths if str(p.mission_task_id) == seen["first_id"]
    ]
    second_paths = [
        p for p in task_paths if str(p.mission_task_id) == seen["second_id"]
    ]
    assert first_paths and second_paths
    assert {p.path_id for p in first_paths}.isdisjoint(
        {p.path_id for p in second_paths}
    )
    first_targets = {
        t for p in first_paths for s in p.steps for t in s.target_node_ids
    }
    second_targets = {
        t for p in second_paths for s in p.steps for t in s.target_node_ids
    }
    assert first_targets.isdisjoint(second_targets)
    first_receipts = {
        e for p in first_paths for s in p.steps for e in s.evidence_ids
    }
    second_receipts = {
        e for p in second_paths for s in p.steps for e in s.evidence_ids
    }
    assert first_receipts
    assert second_receipts
    assert first_receipts.isdisjoint(second_receipts)


async def test_optional_pending_task_never_emits_false_completion() -> None:
    llm = TaskBoundaryLLM(
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=h.FakeEvidenceStore(),
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
    mission = await deps.task_graph_store.get_mission(
        runtime.mission_id
    )
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED
    optional = await deps.task_graph_store.get_task(
        TaskId(seen["optional_id"])
    )
    assert optional is not None
    assert optional.status is not TaskStatus.COMPLETED
    completed = event_bus.history(event_type=EventType.TASK_COMPLETED)
    done_ids = [str(e.payload.get("task_id")) for e in completed]
    assert seen["optional_id"] not in done_ids
    assert seen["required_id"] in done_ids


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
    runtime = h.make_runtime(
        "run-receipt-guard", "eng-t1", mission_id="m-guard"
    )
    runtime = runtime.model_copy(
        update={"active_task_id": TaskId("task-guard")}
    )
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
                completion_criteria=[CompletionCriterion(
                    kind="evidence", description="x", min_evidence=1
                )],
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
                completion_criteria=[CompletionCriterion(
                    kind="evidence", description="x", min_evidence=1
                )],
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
    authz_id = endpoint_node_id(
        "GET", "/api/orders/1234", host="a.local"
    )
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
    compiled_ok, errors_ok = await engine.compile_actions(
        path, [neutral], runtime, context
    )
    assert errors_ok == [], f"neutral read blocked: {errors_ok}"
    assert len(compiled_ok) == 1
    compiled_bad, errors_bad = await engine.compile_actions(
        path, [blocked], runtime, context
    )
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
    assert "operator_excluded_area" in await validate_current_action(
        deps, runtime, bad_step
    )
