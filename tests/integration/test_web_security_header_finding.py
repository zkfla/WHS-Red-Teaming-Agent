"""Web header observation over the native model/tool loop (scripted model, no network).

Real MCP schemas, changing responses and evidence-backed observation boundaries.
Every turn is driven by an explicit scripted native model through the production
MODEL -> TOOLS -> SETTLE path. Nothing runs on a rule fallback: the model answers
native tool calls and genuine completion reviews, and an unscripted route raises
instead of returning an empty decision.

Preserved: actual response kept, observation-only boundary, no findings
invented, evidence-backed provenance. Retired: exact 2-evidence count (now
tool evidences filtered), exact no_actionable_plan ending (now goal close).
Legacy strategy scripting retired with the loop migration (no contract lost):
path_draft / path_review / next_action modes / complete_step intents are gone;
the native model calls the exposed recon/web tools directly and settles through
the task/goal settlement helpers. complete_structured stays only for the genuine
reviews (analysis_verify, task_completion_review, goal_completion_review).
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest
from mcp import Client

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.servers import recon, web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus, Verdict
from redteam.core.ids import EngagementId, ToolId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.analysis import VerificationAssessment
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
from tests.integration import _engine_llm_harness as h


class ChangingResponses(DummyToolGateway):
    def __init__(self, status, header):
        super().__init__()
        self.status = status
        self.header = header

    async def invoke(self, invocation):
        result = await super().invoke(invocation)
        is_web = invocation.tool_id == "web.http_request"
        status = self.status if is_web else 200
        output = {
            "target_ref": invocation.arguments["target_ref"],
            "status_code": status,
            "body_bytes": 0,
            "body_sha256": "a" * 64,
            "redirect": 300 <= status < 400,
            "security_headers_present": {},
            "body_omitted": True,
        }
        if is_web:
            output["session_ref"] = invocation.arguments.get("session_ref")
        if self.header is not None:
            output["security_headers_present"] = {"strict-transport-security": self.header}
        return result.model_copy(update={"output": output})


class OverconfidentVerifier:
    async def verify(self, request):
        return VerificationAssessment(
            verdict=Verdict.VIOLATION if request.proposal.capability == "web" else Verdict.PASS,
            reason="should not promote an observation",
            reviewer_id="test",
        )


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


class _HeaderNativeLLM:
    """Two native tool turns (recon probe, web request), then settlement.

    Only complete_native drives the loop. complete_structured answers the
    genuine reviews (verifier meaning, task settlement, goal settlement);
    any legacy strategy route raises so a silent JSON fallback can never
    stand in for a native turn.
    """

    def __init__(self) -> None:
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
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "header tool result observed",
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
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": item,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "header tool result observed",
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
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""

    @staticmethod
    def _wanted(tools: list[Any]) -> tuple[str, str, str, str]:
        names = _exposed_names(tools)
        recon = exposed_tool_name("recon.http_probe")
        web = exposed_tool_name("web.http_request")
        task_settle = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_settle = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert recon in names, "native model needs the registered recon probe tool"
        assert web in names, "native model needs the registered web request tool"
        assert task_settle in names, "native model needs the task settlement helper"
        assert goal_settle in names, "native model needs the goal settlement helper"
        return recon, web, task_settle, goal_settle

    def _record(self, names: list[str]) -> None:
        self.calls.append({"turn": self.turns, "tool_calls": list(names)})

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        recon, web, task_tool, goal_tool = self._wanted(tools)
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns == 1:
            self._record([recon])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-recon-1",
                        name=recon,
                        arguments={"target_ref": "bank-lab"},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            self._record([web])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-web-1",
                        name=web,
                        arguments={"target_ref": "bank-lab", "path": "/"},
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            assert len(evidenced) >= 2, "task settlement needs both tool evidences"
            self._task_call = "call-task"
            self._record([task_tool])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "header observation evidence ready",
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
            self._goal_call = "call-goal"
            self._record([goal_tool])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "header observation evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


@pytest.mark.parametrize("status,header", [(200, False), (401, False), (403, True), (200, None)])
@pytest.mark.parametrize("fanout", [1, 2])
async def test_web_observation_keeps_actual_response_and_evidence(status, header, fanout):
    policy = ServerPolicy.model_validate(
        {
            "targets": {"bank-lab": {"origin": "http://mock-bank:8080", "allowed_paths": ["/"]}},
            "allowed_networks": ["127.0.0.0/8"],
        }
    )
    registry = MCPToolRegistry()
    bindings = {}
    for namespace, server in [
        ("recon", recon.create_server(policy)),
        ("web", web_http.create_server(policy)),
    ]:
        async with Client(server) as client:
            listing = await client.list_tools()
        for tool in listing.tools:
            if tool.name not in {"http_probe", "dns_resolve", "http_request"}:
                continue
            tool_id = f"{namespace}.{tool.name}"
            registry.register(
                ToolDescriptor(
                    tool_id=ToolId(tool_id),
                    server_id=namespace,
                    tool_name=tool.name,
                    input_schema=tool.input_schema,
                    capability_tags=[namespace],
                    health_status=ToolHealthStatus.HEALTHY,
                )
            )
            bindings[tool_id] = ["service_discovery" if namespace == "recon" else "http_request"]
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("web.browser_navigate"),
            server_id="browser",
            tool_name="browser_navigate",
            input_schema={
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string"}},
            },
            capability_tags=["web"],
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    bindings["web.browser_navigate"] = ["http_request"]
    catalog = MCPToolGateway(registry, {}, bindings)
    engagement = Engagement(
        engagement_id=EngagementId("eng_observation"),
        name="observation",
        scope=EngagementScope(allowed_hosts=["mock-bank"]),
        metadata={"target_refs": {"mock-bank": "bank-lab"}},
        execution_budget=ExecutionBudget(max_actions=10),
        created_at=datetime.now(timezone.utc),
        approval_policy=ApprovalPolicy(),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
    )
    gateway = ChangingResponses(status, header)
    llm = _HeaderNativeLLM()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=catalog,
        llm_client=llm,
        verifier=OverconfidentVerifier(),
        settings=OrchestrationSettings(max_steps=20, max_actions_per_turn=fanout),
    )
    # Production wires the engagement onto deps (alias resolution for mapped
    # refs); the fixture helper leaves it empty, which rejects mapped refs.
    deps = replace(deps, engagement=engagement, local_tools=_native_tool_registry())
    final = await run_engagement(engagement, deps=deps)
    got = [str(i.tool_id) for i in gateway.invocations]
    assert got[:2] == ["recon.http_probe", "web.http_request"]
    assert all(i.arguments["target_ref"] == "bank-lab" for i in gateway.invocations)
    assert all("message" not in i.arguments for i in gateway.invocations)
    # Retired: exact UNKNOWN + no_actionable_plan assumed auto stop.
    # Preserved: observation never promoted to violation despite overconfident verifier.
    assert final.last_verdict in (Verdict.UNKNOWN, Verdict.PASS)
    service = AttackGraphService(deps.graph_store)
    assert await service.list_findings() == []
    node = await service.get_node(endpoint_node_id("GET", "/", host="mock-bank"))
    assert node is not None
    assert attrs_of(node.properties)["status_code"] == status
    assert facts_of(node.properties)["assessment_kind"] == "observation"
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    tool_evidence = [
        e for e in evidence if isinstance(getattr(e, "metadata", {}).get("tool_result"), dict)
    ]
    assert len(tool_evidence) >= 2
    evidence_ids = {str(e.evidence_id) for e in tool_evidence}
    assert set(facts_of(node.properties).get("evidence_ids", [])) <= evidence_ids
    assert all(
        edge.properties["provenance"][0]["evidence_id"] in evidence_ids
        for edge in await service.list_edges()
        if edge.type.value in {"hosts", "exposes"}
    )
    observed = deps.event_bus.history(event_type="ObservationRecorded")
    assert len(observed) == 2
    assert {event.payload["action_id"] for event in observed} == {
        str(invocation.action_id) for invocation in gateway.invocations
    }
    assert observed[0].payload["evidence_id"] in evidence_ids
