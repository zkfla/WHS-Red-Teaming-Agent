"""Operator endpoint exclusion is enforced at both executable frontiers."""

from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.attack_graph.identity import endpoint_node_id, host_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import AgentPhase, GraphNodeType, MissionStatus
from redteam.core.ids import EngagementId, MissionId, RunId
from redteam.orchestration.events import CANDIDATE_REJECTED
from redteam.orchestration.nodes import plan_node
from redteam.orchestration.operator_intents.suppression import endpoint_excluded
from redteam.orchestration.replanning import replan_task_graph
from redteam.orchestration.state import RuntimeState, initial_graph_state
from tests.fakes import build_test_application


def test_path_exclusion_is_exact_and_url_exclusion_is_authority_bound() -> None:
    endpoint = endpoint_node_id("GET", "/api/docs", host="a.local")
    assert endpoint_excluded(endpoint, ["/api/docs"])
    assert endpoint_excluded(endpoint, ["https://a.local/api/docs"])
    assert not endpoint_excluded(endpoint, ["/api/doc"])
    assert not endpoint_excluded(endpoint, ["/api/docs-old"])
    assert not endpoint_excluded(endpoint, ["https://b.local/api/docs"])
    assert not endpoint_excluded(host_node_id("a.local"), ["/api/docs"])
    assert endpoint_excluded(endpoint_node_id("GET", "/orders/1234", host="a.local"),
                             ["/orders/1234"])


async def _mission(deps, excluded: list[str]) -> MissionId:
    mission_id = MissionId("mission_operator_excluded")
    now = datetime.now(timezone.utc)
    await deps.task_graph_store.create_mission(
        MissionPlan(
            mission_id=mission_id, engagement_id=EngagementId("eng_operator_excluded"),
            objective="Assess existing endpoints", status=MissionStatus.ACTIVE,
            operator_preferences={"excluded_endpoints": excluded},
            provenance=Provenance(source="test"), created_at=now, updated_at=now,
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

    class Publisher:
        async def publish(self, *args, **kwargs):
            return SimpleNamespace(event_id="evt_operator_excluded")

    await replan_task_graph(
        deps.task_graph_store, graph, mission_id,
        publisher=Publisher(), last_event_id=None,
    )
    tasks = await deps.task_graph_store.list_tasks(mission_id=mission_id)
    assert tasks
    assert all("/api/orders/{id}" not in task.title for task in tasks)
    assert any("/api/products/{id}" in task.title for task in tasks)


async def test_excluded_authorization_area_does_not_regenerate_authz_work() -> None:
    deps = build_test_application()
    mission_id = await _mission(deps, [])
    mission = await deps.task_graph_store.get_mission(mission_id)
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

    class Publisher:
        async def publish(self, *args, **kwargs):
            return SimpleNamespace(event_id="evt_operator_excluded")

    await replan_task_graph(
        deps.task_graph_store, graph, mission_id,
        publisher=Publisher(), last_event_id=None,
    )
    assert await deps.task_graph_store.list_tasks(mission_id=mission_id) == []


async def test_excluded_endpoint_candidate_is_rejected_before_action_proposal() -> None:
    deps = build_test_application()
    mission_id = await _mission(deps, ["/api/docs"])
    graph = AttackGraphService(deps.graph_store)
    await graph.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"in_scope": True})
    await graph.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_node_id("GET", "/api/docs", host="a.local"),
        attrs={"status_code": 200, "web_inspected": True},
        facts={
            "host": "a.local", "path": "/api/docs",
            "method": "GET", "observed_target": "/api/docs",
        },
    )
    runtime = RuntimeState(
        run_id=RunId("run_operator_excluded"),
        engagement_id=EngagementId("eng_operator_excluded"),
        mission_id=mission_id, phase=AgentPhase.PLAN,
        current_agent=deps.agents.get("web-api-agent").agent_id,
    )
    state = initial_graph_state(runtime)
    state["observation"] = await graph.observe(step=1)
    planned = await plan_node(state, deps=deps)

    assert planned["action_proposal"] is None
    assert any(
        event.payload["reason"] == "operator_excluded_endpoint"
        for event in deps.event_bus.history(event_type=CANDIDATE_REJECTED)
    )
