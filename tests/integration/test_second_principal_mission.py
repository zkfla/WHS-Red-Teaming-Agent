"""A configured pair goes through Control, a tool result, Evidence and replanning.

Migration: retired auto-planner assumption. Explicit scripted model supplies
the compare plan; no rule restoration, no guard monkeypatch. Preserved:
second-principal acquisition, object-bound auth task, evidence-backed
comparison, no invented findings. Retired: implicit frontier assertions.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

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
from redteam.core.enums import GraphNodeType, MissionStatus, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.operator_intents.store import SQLiteOperatorIntentStore
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.fakes import build_test_application
from tests.integration import _engine_llm_harness as h
from tests.integration.test_authentication_followup import SessionGateway


class _CompareLLM(h.ScriptedLLM):
    def __init__(self, case: str = "success") -> None:
        super().__init__()
        self.case = case
        self._graph: Any = None
        self._evidence: Any = None
        self._next_calls = 0

    async def _host_id(self) -> str:
        nodes = await self._graph.list_nodes()
        for n in nodes:
            if n.type is GraphNodeType.HOST and str(n.id) == "host:bank.local":
                return str(n.id)
        host = next(n for n in nodes if n.type is GraphNodeType.HOST)
        return str(host.id)

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    def _draft(self, host_id: str) -> dict[str, Any]:
        def _crit() -> dict[str, Any]:
            return {"kind": "evidence", "description": "result", "min_evidence": 1}

        # Hold cases never act, so the draft claims no principals it cannot
        # name; the rejection proof is no invocations plus a held run.
        refs = [] if self.case in ("no_sessions", "wrong_path") else ["alice", "bob"]
        return {
            "reason": "scripted compare draft",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "compare two sessions",
                    "agent_id": "web-api-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": refs,
                    "allowed_operations": ["web.response_analysis"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [_crit()],
                    "required": True,
                },
            ],
        }

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "path_draft":
            return self._draft(await self._host_id())
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
            review_payload = self._payload(messages)
            if isinstance(review_payload.get("task"), dict):
                excerpts = review_payload.get("evidence_excerpts", [])
                task_ids = [
                    str(item.get("evidence_id", "")).strip()
                    for item in excerpts
                    if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
                ]
                assert task_ids, "task review needs related stored evidence"
                return h.task_accept(task_ids, reason="compare task done")
            return h.completion_accept()
        if route == "goal_completion_review":
            payload = self._payload(messages)
            criteria = payload["mission"]["success_criteria"]
            receipts: list[str] = []
            for step in payload["path"]["steps"]:
                receipts.extend(step.get("evidence_ids", []))
            return h.goal_accept(
                [{"criterion": c, "met": True, "evidence_ids": list(receipts)} for c in criteria]
            )
        if route == "next_action":
            if self.case in ("no_sessions", "wrong_path"):
                return {
                    "mode": "hold",
                    "reason": "no usable sessions",
                    "actions": [],
                    "path": None,
                    "path_step_id": None,
                    "completion": {},
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            self._next_calls += 1
            host_id = await self._host_id()
            if self._next_calls == 1:
                return {
                    "mode": "actions",
                    "reason": "compare",
                    "actions": [
                        {
                            "path_step_id": "step-1",
                            "agent_id": "web-api-agent",
                            "target_node_id": host_id,
                            "principal_ref": "alice",
                            "capability": "web",
                            "operation": "response_analysis",
                            "tool_id": "web.http_compare_sessions",
                            "arguments": {
                                "target_ref": "bank",
                                "path": "/orders/1234",
                                "first_session_ref": "alice",
                                "second_session_ref": "bob",
                            },
                            "reason": "compare alice vs bob",
                            "expected_observation": "comparison recorded",
                            "hypothesis": None,
                            "existing_hypothesis_id": None,
                            "expectation": None,
                            "authentication_check": None,
                            "availability_impact": False,
                        }
                    ],
                    "path": None,
                    "path_step_id": None,
                    "completion": {},
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            payload = self._payload(messages)
            run_id = payload["active_path"]["run_id"]
            records = await self._evidence.list_for_run(run_id)
            tool_ids = [
                str(r.evidence_id)
                for r in records
                if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            if self._next_calls == 2:
                if not tool_ids:
                    return {
                        "mode": "hold",
                        "reason": "awaiting evidence",
                        "actions": [],
                        "path": None,
                        "path_step_id": None,
                        "completion": {},
                        "evidence_request": {},
                        "hypothesis_update": {},
                    }
                # Only a conclusive two-session comparison closes the step;
                # inconclusive receipts (same identity, wrong target, error
                # statuses, unavailable) hold so the mission stays blocked.
                if not await self._comparison_conclusive():
                    return {
                        "mode": "hold",
                        "reason": "comparison inconclusive",
                        "actions": [],
                        "path": None,
                        "path_step_id": None,
                        "completion": {},
                        "evidence_request": {},
                        "hypothesis_update": {},
                    }
                # Cross-step guard: only receipts from this step close it.
                records = await self._evidence.list_for_run(run_id)
                owned = [
                    str(r.evidence_id)
                    for r in records
                    if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
                    and r.metadata.get("path_step_id") == "step-1"
                    and str(getattr(r, "metadata", {}).get("tool_id", r.source_id))
                    == "web.http_compare_sessions"
                ]
                if not owned:
                    return {
                        "mode": "hold",
                        "reason": "awaiting step-1 evidence",
                        "actions": [],
                        "path": None,
                        "path_step_id": None,
                        "completion": {},
                        "evidence_request": {},
                        "hypothesis_update": {},
                    }
                return {**h.turn_complete_step(owned), "path_step_id": "step-1"}
            if payload.get("active_task"):
                step_receipts = [
                    str(r.evidence_id)
                    for r in records
                    if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
                    and r.metadata.get("path_step_id") == "step-1"
                    and str(getattr(r, "metadata", {}).get("tool_id", r.source_id))
                    == "web.http_compare_sessions"
                ]
                assert step_receipts, "task completion needs related stored evidence"
                return {
                    "mode": "complete_task",
                    "reason": "compare task evidence ready",
                    "actions": [],
                    "path": None,
                    "path_step_id": None,
                    "completion": {
                        "evidence_ids": step_receipts,
                        "outcome": "success",
                        "reason": "compare task evidence ready",
                        "criterion_results": [],
                    },
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            return h.turn_complete_goal()
        raise AssertionError(f"no compare answer for {route!r}")

    async def complete(self, *, messages: Any, system: Any = None, **_: Any) -> str:
        return ""

    async def _comparison_conclusive(self) -> bool:
        from redteam.attack_graph.predicates import facts_of as _facts_of

        try:
            nodes = await self._graph.list_nodes()
        except Exception:
            return False
        for node in nodes:
            if node.type is not GraphNodeType.ENDPOINT:
                continue
            observations = _facts_of(node.properties).get("observations", [])
            for item in observations:
                if not isinstance(item, dict):
                    continue
                if item.get("kind") != "http_session_comparison":
                    continue
                first = item.get("first_status")
                second = item.get("second_status")
                if type(first) is not int or type(second) is not int:
                    continue
                if item.get("target_ref") != "bank":
                    continue
                if item.get("first_principal_ref") == item.get("second_principal_ref"):
                    continue
                if {item.get("first_session_ref"), item.get("second_session_ref")} != {
                    "alice",
                    "bob",
                }:
                    continue
                if (200 <= first < 300 and (200 <= second < 300 or second == 403)) or (
                    200 <= second < 300 and first == 403
                ):
                    return True
        return False


class ComparisonGateway(SessionGateway):
    def __init__(self, case="success") -> None:
        super().__init__(200, 200)
        self.case = case
        self._tools.append(
            ToolDescriptor(
                tool_id=ToolId("web.http_compare_sessions"),
                server_id="test",
                tool_name="http_compare_sessions",
                operations=["response_analysis"],
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
    llm = _CompareLLM(case)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=35),
    )
    # Production wires the engagement onto deps; without it mapped refs
    # (bank.local -> bank) fail alias checks at action-compile time.
    deps = replace(deps, engagement=engagement)
    llm._graph = deps.graph_store
    llm._evidence = deps.evidence_store
    if task_db is not None:
        deps = replace(
            deps,
            task_graph_store=SQLiteTaskGraphStore(task_db),
            path_store=SQLitePathStore(task_db),
            operator_intent_store=SQLiteOperatorIntentStore(task_db),
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
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    # Retired: automatic replan tasks no longer exist in production; the
    # durable proof is the explicit structured path plus the comparison
    # evidence itself.
    assert final.termination_reason is TerminationReason.GOAL_ACHIEVED
    assert final.mission_status is MissionStatus.COMPLETED
    assert mission is not None and mission.status is MissionStatus.COMPLETED
    assert mission.criterion_results and all(r.met for r in mission.criterion_results)
    assert any(str(i.tool_id) == "web.http_compare_sessions" for i in gateway.invocations)
    invoked = next(i for i in gateway.invocations if str(i.tool_id) == "web.http_compare_sessions")
    assert invoked.arguments["target_ref"] == "bank"
    assert invoked.arguments["path"] == "/orders/1234"
    assert {invoked.arguments["first_session_ref"], invoked.arguments["second_session_ref"]} == {
        "alice",
        "bob",
    }
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    comparisons = [
        e
        for e in evidence
        if e.source_id == "web.http_compare_sessions"
        and isinstance(e.metadata.get("session_comparison"), dict)
    ]
    assert comparisons, "no durable comparison receipt"
    endpoint = await graph.get_node(endpoint_id)
    assert endpoint is not None
    assert any(
        item.get("kind") == "http_session_comparison"
        for item in facts_of(endpoint.properties).get("observations", [])
    )
    principals = [
        n
        for n in await graph.list_nodes()
        if n.type is GraphNodeType.PRINCIPAL and facts_of(n.properties).get("target_ref") == "bank"
    ]
    assert len(principals) >= 2, "both session principals must be observed"
    assert await graph.list_findings() == []
    path = await deps.path_store.get_active(str(final.run_id))
    assert path is not None
    assert path.review_records.get("decision") == "accept"
    assert any(s.evidence_ids for s in path.steps)


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
    # Retired: automatic replan tasks no longer exist; the negative proof
    # is a held run, unmet criteria, and no invented findings.
    assert final.termination_reason is TerminationReason.INVESTIGATION_HELD
    assert final.mission_status is MissionStatus.BLOCKED
    assert mission is not None and mission.status is MissionStatus.BLOCKED
    assert mission.criterion_results and not all(r.met for r in mission.criterion_results)
    assert await graph.list_findings() == []
    if case in {"no_sessions", "wrong_path"}:
        assert not any(str(i.tool_id) == "web.http_compare_sessions" for i in gateway.invocations)
    else:
        # Negative receipt: the comparison ran but never completes the goal.
        assert any(str(i.tool_id) == "web.http_compare_sessions" for i in gateway.invocations)


async def test_known_principals_still_execute_endpoint_comparison():
    final, deps, gateway, graph, endpoint_id = await _exercise("known_principals")
    # Retired: automatic acquisition tasks no longer exist; the durable
    # proof is the executed comparison plus the structured path receipts.
    assert final.termination_reason is TerminationReason.GOAL_ACHIEVED
    assert final.mission_status is MissionStatus.COMPLETED
    assert any(str(i.tool_id) == "web.http_compare_sessions" for i in gateway.invocations)
    endpoint = await graph.get_node(endpoint_id)
    assert endpoint is not None
    assert any(
        item.get("kind") == "http_session_comparison"
        for item in facts_of(endpoint.properties).get("observations", [])
    )
    path = await deps.path_store.get_active(str(final.run_id))
    assert path is not None
    assert path.review_records.get("decision") == "accept"
    assert any(s.evidence_ids for s in path.steps)


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
