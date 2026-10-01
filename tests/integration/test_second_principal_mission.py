"""A configured pair goes through Control, a tool result, Evidence and replanning."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, MissionStatus, TaskStatus, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import build_test_application
from tests.integration.test_authentication_followup import SessionGateway


class ComparisonGateway(SessionGateway):
    def __init__(self) -> None:
        super().__init__(200, 200)
        self._tools.append(
            ToolDescriptor(
                tool_id=ToolId("web.http_compare_sessions"),
                server_id="test",
                tool_name="http_compare_sessions",
                capability_tags=["web"],
                health_status=ToolHealthStatus.HEALTHY,
                input_schema={
                    "type": "object",
                    "required": ["target_ref", "path", "first_session_ref", "second_session_ref"],
                    "properties": {
                        key: {"type": "string"}
                        for key in ("target_ref", "path", "first_session_ref", "second_session_ref")
                    },
                    "additionalProperties": False,
                },
            )
        )

    async def invoke(self, invocation):
        result = await super().invoke(invocation)
        if invocation.tool_id != "web.http_compare_sessions":
            return result
        assert invocation.arguments["target_ref"] == "bank"
        assert invocation.arguments["path"] == "/orders/1234"
        assert {
            invocation.arguments["first_session_ref"],
            invocation.arguments["second_session_ref"],
        } == {"alice", "bob"}
        return result.model_copy(
            update={
                "output": {
                    "target_ref": "bank",
                    "first": {
                        "principal_ref": "alice-principal",
                        "status_code": 200,
                        "body_sha256": "a" * 64,
                    },
                    "second": {
                        "principal_ref": "bob-principal",
                        "status_code": 403,
                        "body_sha256": "b" * 64,
                    },
                    "same_status": False,
                    "same_body": False,
                    "verdict": "observation_only",
                    "request_count": 2,
                }
            }
        )


async def test_registered_second_principal_is_acquired_and_replanned() -> None:
    engagement = Engagement(
        engagement_id=EngagementId("eng_pair"),
        name="pair",
        scope=EngagementScope(allowed_hosts=["bank.local"]),
        metadata={
            "target_refs": {"bank.local": "bank"},
            "auth_sessions": {
                "bank.local": [
                    {"session_ref": "alice", "paths": ["/orders/1234"]},
                    {"session_ref": "bob", "paths": ["/orders/1234"]},
                ]
            },
        },
        execution_budget=ExecutionBudget(max_actions=12),
        approval_policy=ApprovalPolicy(),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        created_at=datetime.now(timezone.utc),
    )
    gateway = ComparisonGateway()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=35),
    )
    graph = AttackGraphService(deps.graph_store)
    endpoint_id = endpoint_node_id("GET", "/orders/1234", host="bank.local")
    await graph.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_id,
        facts={
            "host": "bank.local",
            "path": "/orders/1234",
            "method": "GET",
            "target_ref": "bank",
            "path_parameters": ["orders_id"],
        },
    )

    final = await run_engagement(engagement, deps=deps, objective="Assess authorization")
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    acquisition = next(t for t in tasks if t.provenance.source == "replan:acquire_second_principal")
    authorization = next(t for t in tasks if t.provenance.source == "replan:object_bound_endpoint")
    assert acquisition.status is TaskStatus.COMPLETED
    assert authorization.status is TaskStatus.COMPLETED
    assert final.mission_status is MissionStatus.COMPLETED, (
        [(t.title, t.status, t.blocking_reason) for t in tasks],
        mission.criterion_results,
    )
    assert mission.criterion_results[0].criterion == "verified evidence for authorization"
    assert mission.criterion_results[0].met is True
    assert mission.criterion_results[0].evidence_ids
    assert any(i.tool_id == "web.http_compare_sessions" for i in gateway.invocations)
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert any(
        e.source_id == "web.http_compare_sessions"
        and e.metadata["mission_task_id"] == acquisition.task_id
        for e in evidence
    )
    endpoint = await graph.get_node(endpoint_id)
    assert any(
        item.get("kind") == "http_session_comparison"
        for item in facts_of(endpoint.properties).get("observations", [])
    )
