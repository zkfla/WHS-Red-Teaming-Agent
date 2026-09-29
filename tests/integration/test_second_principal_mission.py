"""A configured pair goes through Control, a tool result, Evidence and replanning."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

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
from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import GraphNodeType, MissionStatus, TaskStatus, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.fakes import build_test_application
from tests.integration.test_authentication_followup import SessionGateway


class ComparisonGateway(SessionGateway):
    def __init__(self, case="success") -> None:
        super().__init__(200, 200)
        self.case = case
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
        result = result.model_copy(
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
        if self.case == "same_principal":
            result.output["second"]["principal_ref"] = "alice-principal"
        elif self.case == "wrong_target":
            result.output["target_ref"] = "other-app"
        elif self.case in {"server_error", "unauthenticated", "both_denied"}:
            status = {"server_error": 500, "unauthenticated": 401, "both_denied": 403}[self.case]
            result.output["first"]["status_code"] = status
            result.output["second"]["status_code"] = status
            result.output["same_status"] = True
        elif self.case == "unavailable":
            result = result.model_copy(
                update={
                    "success": False,
                    "output": None,
                    "error": ToolError(
                        code="session_unavailable", message="registered session unavailable"
                    ),
                }
            )
        return result


async def _exercise(case="success", task_db=None):
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
    if case == "no_sessions":
        engagement.metadata["auth_sessions"] = {}
    elif case == "wrong_path":
        for session in engagement.metadata["auth_sessions"]["bank.local"]:
            session["paths"] = ["/other"]
    gateway = ComparisonGateway(case)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=35),
    )
    if task_db is not None:
        deps = replace(deps, task_graph_store=SQLiteTaskGraphStore(task_db))
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
    if case == "known_principals":
        for ref in ("alice", "bob"):
            await graph.upsert_node(
                GraphNodeType.PRINCIPAL,
                key=f"bank/{ref}",
                facts={"target_ref": "bank", "principal_ref": f"{ref}-principal"},
            )

    final = await run_engagement(engagement, deps=deps, objective="Assess authorization")
    return final, deps, gateway, graph, endpoint_id


async def test_registered_second_principal_is_acquired_and_replanned() -> None:
    final, deps, gateway, graph, endpoint_id = await _exercise()
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


@pytest.mark.parametrize(
    "case",
    [
        "no_sessions",
        "wrong_path",
        "same_principal",
        "wrong_target",
        "server_error",
        "unauthenticated",
        "both_denied",
        "unavailable",
    ],
)
async def test_unusable_principals_never_complete_mission(case):
    final, deps, gateway, graph, _ = await _exercise(case)
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    authorization = next(t for t in tasks if t.provenance.source == "replan:object_bound_endpoint")
    assert final.mission_status is MissionStatus.BLOCKED
    assert authorization.status is TaskStatus.BLOCKED
    assert authorization.blocking_reason
    assert mission.criterion_results and not mission.criterion_results[0].met
    assert await graph.list_findings() == []
    acquisition = next(t for t in tasks if t.provenance.source == "replan:acquire_second_principal")
    assert acquisition.status is TaskStatus.BLOCKED
    if case in {"no_sessions", "wrong_path"}:
        assert not any(i.tool_id == "web.http_compare_sessions" for i in gateway.invocations)
        assert "session references" in acquisition.blocking_reason


async def test_known_principals_still_execute_endpoint_comparison():
    final, deps, gateway, _, _ = await _exercise("known_principals")
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    assert not any(t.provenance.source == "replan:acquire_second_principal" for t in tasks)
    assert final.mission_status is MissionStatus.COMPLETED
    assert any(i.tool_id == "web.http_compare_sessions" for i in gateway.invocations)


@pytest.mark.parametrize("case", ["success", "server_error"])
async def test_criterion_results_survive_sqlite_reopen(tmp_path, case):
    path = tmp_path / "tasks.sqlite"
    final, deps, _, _, _ = await _exercise(case, task_db=path)
    expected = await deps.task_graph_store.get_mission(final.mission_id)
    restored = await SQLiteTaskGraphStore(path).get_mission(final.mission_id)
    assert restored.criterion_results == expected.criterion_results
    assert restored.status is (
        MissionStatus.COMPLETED if case == "success" else MissionStatus.BLOCKED
    )
    assert restored.criterion_results[0].met is (case == "success")
