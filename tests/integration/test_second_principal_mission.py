"""Two registered sessions go through the native model/tool loop and settle.

Native-loop migration: the scripted model answers ``complete_native`` only,
calling the exposed compare tool and settling through the real
task.settle/goal.settle local tools. ``complete_structured`` answers just the
genuine review routes (analysis verification plus task/goal settlement
reviews); legacy strategy routes raise. Preserved: second-principal
acquisition, evidence-backed comparison, no invented findings. Retired:
implicit frontier assertions and the path-record proof (settlement receipts
carry the contract now).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import (
    GraphNodeType,
    MissionStatus,
    TaskStatus,
    ToolHealthStatus,
)
from redteam.core.ids import EngagementId, ToolId
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
from redteam.orchestration.operator_intents.store import SQLiteOperatorIntentStore
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes import FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h
from tests.integration.test_authentication_followup import SessionGateway

COMPARE_TOOL_ID = "web.http_compare_sessions"


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


class _CompareNativeLLM:
    """Scripted native model: one compare call, then settlement or hold.

    Only ``complete_native`` drives the loop. ``complete_structured``
    answers the genuine review routes the execution path legitimately
    makes (analysis verification plus task/goal settlement reviews); any
    legacy strategy route raises so a retired JSON plan can never stand
    in for a native turn. Cases without usable sessions never act, and an
    inconclusive comparison holds, so the mission stays blocked.
    """

    def __init__(self, case: str = "success") -> None:
        self.case = case
        self._graph: Any = None
        self._evidence: Any = None
        self.turns = 0
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self._task_call: str | None = None
        self._goal_call: str | None = None

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
                "reason": "compare task evidence ready",
                "claims": [
                    {
                        "description": "two-session comparison observed in stored evidence",
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
                "reason": "compare goal evidence ready",
                "criteria": [
                    {
                        "criterion": item,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "two-session comparison in stored evidence",
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
        raise AssertionError(
            f"legacy strategy route is not part of the native loop: {route!r}"
        )

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        compare = exposed_tool_name(COMPARE_TOOL_ID)
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert compare in names, "native model needs the registered compare tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        if self.turns == 1:
            self.calls.append({"turn": 1, "tool_calls": []})
            if self.case in ("no_sessions", "wrong_path"):
                return NativeCompletion(tool_calls=(), text="no usable sessions")
            self.calls[-1]["tool_calls"] = [compare]
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-compare",
                        name=compare,
                        arguments={
                            "target_ref": "bank",
                            "path": "/orders/1234",
                            "first_session_ref": "alice",
                            "second_session_ref": "bob",
                        },
                    ),
                ),
                text="",
            )
        evidenced = _remote_executed_evidence(messages)
        if self.case in ("no_sessions", "wrong_path"):
            return NativeCompletion(tool_calls=(), text="no usable sessions")
        if not evidenced:
            return NativeCompletion(tool_calls=(), text="awaiting compare evidence")
        # Only a conclusive two-session comparison settles the task and the
        # goal; inconclusive receipts (same identity, wrong target, error
        # statuses, unavailable) hold so the mission stays blocked.
        if not await self._comparison_conclusive():
            return NativeCompletion(tool_calls=(), text="comparison inconclusive")
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-task"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "compare task evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="awaiting task settlement")
        if self._goal_call is None:
            self._goal_call = "call-goal"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "compare goal evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")

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
        assert invocation.arguments["first_session_ref"] == "alice"
        assert invocation.arguments["second_session_ref"] == "bob"
        # Mock response receipts for the declared simulated sides
        # (alice -> first, bob -> second). Mock response receipt, no actual HTTP.
        result = result.model_copy(
            update={
                "output": {
                    "target_ref": "bank",
                    "first": {
                        "principal_ref": "alice-principal",
                        "status_code": 200,
                        "body_sha256": "a" * 64,
                        "actual_session_ref": "alice",
                    },
                    "second": {
                        "principal_ref": "bob-principal",
                        "status_code": 403,
                        "body_sha256": "b" * 64,
                        "actual_session_ref": "bob",
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
    llm = _CompareNativeLLM(case)
    if task_db is not None:
        task_store: Any = SQLiteTaskGraphStore(task_db)
        path_store: Any = SQLitePathStore(task_db)
        intent_store: Any = SQLiteOperatorIntentStore(task_db)
    else:
        task_store = InMemoryTaskGraphStore()
        path_store = InMemoryPathStore()
        intent_store = None
    deps = RuntimeDeps(
        control_plane=FakeControlPlane(),
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
        task_graph_store=task_store,
        path_store=path_store,
        operator_intent_store=intent_store,
        settings=OrchestrationSettings(max_steps=12, checkpointing=False),
    )
    llm._graph = deps.graph_store
    llm._evidence = deps.evidence_store
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
    # The durable proof is the executed comparison plus the settlement
    # receipts: the task settled from the compare evidence and the goal
    # settled from the same executed origins.
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
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    settled = [task for task in tasks if task.status is TaskStatus.COMPLETED]
    assert settled, "goal settlement keeps its completed task"
    assert all(task.objective == mission.objective for task in settled)
    tool_ids = {
        str(record.evidence_id)
        for record in evidence
        if isinstance(record.metadata.get("tool_result"), dict)
    }
    assert tool_ids, "settlement needs the executed compare call evidence"
    for result in mission.criterion_results:
        assert result.met and result.evidence_ids
        assert set(map(str, result.evidence_ids)) <= tool_ids
        assert result.result_claims, "each criterion keeps its actual result claims"
    llm = deps.llm_client
    assert isinstance(llm, _CompareNativeLLM)
    assert {"task_completion_review", "goal_completion_review"} <= set(llm.reviews)


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
    # The negative proof is a held run, unmet criteria, and no invented
    # findings: inconclusive comparisons never settle the task or the goal.
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
    # The durable proof is the executed comparison plus the settlement
    # receipts: known principals still run the endpoint comparison.
    assert final.termination_reason is TerminationReason.GOAL_ACHIEVED
    assert final.mission_status is MissionStatus.COMPLETED
    assert any(str(i.tool_id) == "web.http_compare_sessions" for i in gateway.invocations)
    endpoint = await graph.get_node(endpoint_id)
    assert endpoint is not None
    assert any(
        item.get("kind") == "http_session_comparison"
        for item in facts_of(endpoint.properties).get("observations", [])
    )
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None and mission.status is MissionStatus.COMPLETED
    assert mission.criterion_results and all(r.met for r in mission.criterion_results)


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
