"""H1 end to end: SERVICE with no ENDPOINT -> Recon recovers -> Web runs.

The starting graph is the state the plan calls stalled: the host probe has
already been attempted (so the playbook's own step is retired and will never
run again), a service is known, and no endpoint was ever produced. Before
this change nothing downstream could proceed, because
WEB_API_AGENT.inspect-http-service cannot fill a path on a ServiceTarget.

This test drives that state through the real loop: candidate -> Control
Plane -> tool call -> Result Mapper -> ENDPOINT -> Web/API reactivation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import host_node_id, service_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
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
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, build_test_application
from tests.fakes.evidence_store import FakeEvidenceStore

pytestmark = pytest.mark.asyncio


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


class _BootstrapLLM:
    """Native script for stalled-service recovery: recon probe then web."""

    def __init__(
        self,
        graph_store: InMemoryAttackGraphStore,
        evidence_store: FakeEvidenceStore,
    ) -> None:
        self._graph = graph_store
        self._evidence = evidence_store
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0
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

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        raise AssertionError("native loop must not use free-text completion")

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        system: str | None = None,
        num_retries: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema or {})
        self.calls.append({"route": route})
        self.reviews.append(route)
        if route == "analysis_verify":
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "scripted",
                "confidence": 0.8,
                "claim_supported": True,
            }
        if route == "task_completion_review":
            assert messages is not None
            payload = json.loads(messages[0]["content"])
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "recon tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            assert messages is not None
            payload = json.loads(messages[0]["content"])
            criteria = payload.get("mission", {}).get("success_criteria", []) or [
                "map the in-scope host"
            ]
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "goal review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": c,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "recon tool result observed",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for c in criteria
                ],
            }
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        recon = exposed_tool_name("recon.http_probe")
        web = exposed_tool_name("web.http_request")
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert recon in names, "native model needs the recon probe tool"
        assert web in names, "native model needs the web request tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns == 1:
            self.calls.append({"turn": self.turns, "tool_calls": [recon]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-recon-1",
                        name=recon,
                        arguments={"target_ref": "a.local", "path": "/"},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            assert evidenced, "web followup needs the recon probe evidence"
            self.calls.append({"turn": self.turns, "tool_calls": [web]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-web-1",
                        name=web,
                        arguments={"target_ref": "a.local", "path": "/", "method": "GET"},
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-task-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "recon and web evidence ready",
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
            self._goal_call = "call-goal-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "recovery evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


_HTTP_PROBE_OUTPUT = {
    "target_ref": "a.local",
    "status_code": 200,
    "body_bytes": 10,
    "body_sha256": "a" * 64,
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
    "body_omitted": True,
}


def _engagement(*hosts: str) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_bootstrap"),
        name="bootstrap",
        scope=EngagementScope(allowed_hosts=list(hosts)),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _gateway() -> DummyToolGateway:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="test",
        tool_name="http_probe",
        operations=['service_discovery', 'endpoint_discovery'],
        input_schema={
            "type": "object",
            "properties": {"target_ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["target_ref"],
        },
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    # Native binding resolves the execution target from arguments plus the
    # descriptor's declared kinds (the legacy path supplied explicit node
    # ids, so these doubles never needed kinds). The recovery probe inspects
    # the discovered endpoint, so the web double declares the endpoint kind.
    web_tool = ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="test",
        tool_name="http_request",
        operations=['http_request'],
        target_types=["endpoint"],
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "path": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "session_ref": {"type": "string"},
            },
            "required": ["target_ref", "path"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    return DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, web_tool])


async def _seed_stalled_service(graph: AttackGraphService) -> None:
    """HOST + HTTP SERVICE, no ENDPOINT, host probe already retired."""

    await graph.upsert_node(
        GraphNodeType.HOST,
        node_id=host_node_id("a.local"),
        attrs={"in_scope": True},
        facts={
            "target_ref": "a.local",
            "attempted_operations": ["recon.service_discovery"],
        },
    )
    await graph.upsert_node(
        GraphNodeType.SERVICE,
        node_id=service_node_id("http", host="a.local"),
        attrs={"responded": True},
        facts={
            "host": "a.local",
            "application_protocol": "http",
            "target_ref": "a.local",
        },
    )


async def test_a_stalled_service_only_graph_is_recovered_and_web_runs() -> None:
    gateway = _gateway()
    graph_store = InMemoryAttackGraphStore()
    evidence_store = FakeEvidenceStore()
    llm = _BootstrapLLM(graph_store=graph_store, evidence_store=evidence_store)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20, decision_max_attempts=6),
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
    )
    deps.local_tools.register(task_settle_descriptor(), task_settle_handler)
    deps.local_tools.register(goal_settle_descriptor(), goal_settle_handler)
    graph = AttackGraphService(deps.graph_store)
    await _seed_stalled_service(graph)

    final = await run_engagement(_engagement("a.local"), deps=deps)

    # 1. The existing catalog-bound probe was the thing that ran. Its path is
    #    the tool's own default, not an invented one.
    probes = [item for item in gateway.invocations if str(item.tool_id) == "recon.http_probe"]
    assert probes, "the bootstrap never reached a real probe"
    assert probes[0].arguments == {"target_ref": "a.local", "path": "/"}

    # 2. The existing Result Mapper turned that into an ENDPOINT.
    endpoints = [node for node in await graph.list_nodes() if node.type is GraphNodeType.ENDPOINT]
    assert endpoints, "the probe did not produce an endpoint"

    # 3. Web/API then used that endpoint, which is the whole point of H1.
    web_calls = [item for item in gateway.invocations if str(item.tool_id) == "web.http_request"]
    assert web_calls, "Web/API Agent never ran against the recovered endpoint"
    assert final.actions_by_agent.get("recon-agent", 0) >= 1
    assert final.actions_by_agent.get("web-api-agent", 0) >= 1


async def test_the_bootstrap_does_not_repeat_once_the_endpoint_exists() -> None:
    # Same recovery, then confirm the recovered state is stable: the endpoint
    # existing is what stops the bootstrap, so it cannot loop.
    gateway = _gateway()
    graph_store = InMemoryAttackGraphStore()
    evidence_store = FakeEvidenceStore()
    llm = _BootstrapLLM(graph_store=graph_store, evidence_store=evidence_store)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20, decision_max_attempts=6),
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
    )
    deps.local_tools.register(task_settle_descriptor(), task_settle_handler)
    deps.local_tools.register(goal_settle_descriptor(), goal_settle_handler)
    graph = AttackGraphService(deps.graph_store)
    await _seed_stalled_service(graph)

    await run_engagement(_engagement("a.local"), deps=deps)

    bootstraps = [
        item
        for item in gateway.invocations
        if str(item.tool_id) == "recon.http_probe" and item.arguments.get("path") == "/"
    ]
    assert len(bootstraps) == 1
