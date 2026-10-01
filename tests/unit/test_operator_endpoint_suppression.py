"""Operator endpoint exclusion is enforced at both executable frontiers."""

from datetime import datetime, timezone

from redteam.attack_graph.identity import endpoint_node_id, host_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import AgentPhase, GraphNodeType, MissionStatus
from redteam.core.ids import EngagementId, MissionId, RunId, ToolId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.operator_intents.suppression import endpoint_excluded
from redteam.orchestration.replanning import replan_task_graph
from redteam.orchestration.state import RuntimeState
from tests.fakes import build_test_application


def test_path_exclusion_is_exact_and_url_exclusion_is_authority_bound() -> None:
    endpoint = endpoint_node_id("GET", "/api/docs", host="a.local")
    assert endpoint_excluded(endpoint, ["/api/docs"])
    assert endpoint_excluded(endpoint, ["https://a.local/api/docs"])
    assert not endpoint_excluded(endpoint, ["/api/doc"])
    assert not endpoint_excluded(endpoint, ["/api/docs-old"])
    assert not endpoint_excluded(endpoint, ["https://b.local/api/docs"])
    assert not endpoint_excluded(host_node_id("a.local"), ["/api/docs"])
    assert endpoint_excluded(
        endpoint_node_id("GET", "/orders/1234", host="a.local"), ["/orders/1234"]
    )


async def _mission(deps, excluded: list[str]) -> MissionId:
    mission_id = MissionId("mission_operator_excluded")
    now = datetime.now(timezone.utc)
    await deps.task_graph_store.create_mission(
        MissionPlan(
            mission_id=mission_id,
            engagement_id=EngagementId("eng_operator_excluded"),
            objective="Assess existing endpoints",
            status=MissionStatus.ACTIVE,
            operator_preferences={"excluded_endpoints": excluded},
            provenance=Provenance(source="test"),
            created_at=now,
            updated_at=now,
        )
    )
    return mission_id


async def test_excluded_endpoint_never_creates_a_new_authz_task() -> None:
    deps = build_test_application()
    mission_id = await _mission(deps, ["/api/orders/1234"])
    graph = AttackGraphService(deps.graph_store)
    for path in ("/api/orders/1234", "/api/products/4567"):
        await graph.upsert_node(
            GraphNodeType.ENDPOINT,
            node_id=endpoint_node_id("GET", path, host="a.local"),
            facts={"path_parameters": ["id"], "host": "a.local", "path": path},
        )

    await replan_task_graph(
        deps.task_graph_store,
        graph,
        mission_id,
        publisher=EventPublisher(
            deps.event_bus,
            run_id=RunId("run_operator_excluded"),
            engagement_id=EngagementId("eng_operator_excluded"),
            source="test",
        ),
        last_event_id=None,
    )
    tasks = await deps.task_graph_store.list_tasks(mission_id=mission_id)
    assert tasks
    assert all("/api/orders/{id}" not in task.title for task in tasks)
    assert any("/api/products/{id}" in task.title for task in tasks)


async def test_excluded_authorization_area_does_not_regenerate_authz_work() -> None:
    deps = build_test_application()
    mission_id = await _mission(deps, [])
    mission = await deps.task_graph_store.get_mission(mission_id)
    assert mission is not None
    await deps.task_graph_store.update_mission(
        mission.model_copy(
            update={
                "revision": mission.revision + 1,
                "operator_preferences": {"excluded_areas": ["authorization"]},
            }
        )
    )
    graph = AttackGraphService(deps.graph_store)
    await graph.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_node_id("GET", "/api/orders/1234", host="a.local"),
        facts={"path_parameters": ["id"], "host": "a.local", "path": "/api/orders/1234"},
    )

    await replan_task_graph(
        deps.task_graph_store,
        graph,
        mission_id,
        publisher=EventPublisher(
            deps.event_bus,
            run_id=RunId("run_operator_excluded"),
            engagement_id=EngagementId("eng_operator_excluded"),
            source="test",
        ),
        last_event_id=None,
    )
    assert await deps.task_graph_store.list_tasks(mission_id=mission_id) == []


async def test_excluded_endpoint_action_is_rejected_at_compilation_and_execution() -> None:
    from redteam.attack_graph.predicates import Confidence
    from redteam.attack_graph.predicates import Provenance as GraphProvenance
    from redteam.orchestration.agents.base import PlanStep
    from redteam.orchestration.engine import (
        ActionIntent,
        InvestigationEngine,
        validate_current_action,
    )
    from redteam.orchestration.paths import CompletionCriterion, PathPlan, PathStep

    deps = build_test_application()
    mission_id = await _mission(deps, ["/api/docs"])
    run_id = RunId("run_operator_excluded")
    node_id = endpoint_node_id("GET", "/api/docs", host="a.local")
    graph = AttackGraphService(deps.graph_store)
    await graph.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=node_id,
        attrs={"in_scope": True},
        facts={"host": "a.local", "path": "/api/docs", "method": "GET"},
        provenance=GraphProvenance(
            step=0,
            source="engagement:eng_operator_excluded",
            confidence=Confidence.SERVER_DECLARED,
            run_id=str(run_id),
        ),
    )
    runtime = RuntimeState(
        run_id=run_id,
        engagement_id=EngagementId("eng_operator_excluded"),
        mission_id=mission_id,
        phase=AgentPhase.PLAN,
    )
    path = PathPlan(
        path_id="path-excluded",
        run_id=str(run_id),
        mission_id=str(mission_id),
        version=1,
        status="active",
        goal="Assess existing endpoints",
        reason="inspect declared endpoint",
        steps=[
            PathStep(
                step_id="check",
                objective="Inspect endpoint",
                target_node_ids=[str(node_id)],
                allowed_operations=["web.http_request"],
                completion_criteria=[CompletionCriterion(kind="evidence")],
            )
        ],
    )
    await deps.path_store.register(path, expected_version=None)
    intent = ActionIntent(
        path_step_id="check",
        agent_id="web-api-agent",
        target_node_id=str(node_id),
        capability="web",
        operation="http_request",
        tool_id="web.http_request",
        arguments={"path": "/api/docs", "method": "GET"},
        reason="inspect endpoint",
    )
    engine = InvestigationEngine(deps)
    context = await engine.context(runtime, await graph.observe(step=1))
    compiled, errors = await engine.compile_actions(path, [intent], runtime, context)
    assert compiled == [] and "operator_excluded_endpoint" in errors
    # Replanning and approval cannot make the same excluded endpoint executable.
    from redteam.core.contracts.target import EndpointTarget

    step = PlanStep(
        step_id="check",
        path_id=path.path_id,
        path_version=path.version,
        path_step_id="check",
        capability="web",
        operation="http_request",
        tool_id=ToolId("web.http_request"),
        target=EndpointTarget(host="a.local", path="/api/docs"),
        arguments=intent.arguments,
        reason=intent.reason,
    )
    assert "operator_excluded_endpoint" in await validate_current_action(deps, runtime, step)
