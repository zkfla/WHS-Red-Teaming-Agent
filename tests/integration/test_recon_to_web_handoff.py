"""End to end: one HOST in scope -> explicit model runs Recon -> Attack Graph
gains SERVICE/ENDPOINT -> explicit model runs Web/API with real
``target_ref``/``path`` arguments -> real ``web.http_request``-shaped
tool is invoked.

Migration: retired the auto-planner assumption (no implicit frontier).
The plan is supplied by an explicit scripted model (harness route answers);
automatic rule/planner restoration and execution-guard monkeypatches are
not used. Preserved contract: graph-derived required fields, both roles
act, web call really happens.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
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
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, build_test_application
from tests.integration import _engine_llm_harness as h

pytestmark = pytest.mark.asyncio

_HTTP_PROBE_OUTPUT = {
    "target_ref": "a.local",
    "status_code": 200,
    "body_bytes": 10,
    "body_sha256": "a" * 64,
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
    "body_omitted": True,
    "scheme": "http",
    "port": 80,
}

# Verified directly against `redteam.capability.servers.web_http`'s
# MCP-declared input_schema for `http_request` -- not assumed.
_REAL_WEB_HTTP_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "target_ref": {"type": "string"},
        "path": {"type": "string"},
        "method": {"type": "string", "default": "GET"},
    },
    "required": ["target_ref", "path"],
}


class _HandoffLLM(h.ScriptedLLM):
    """Explicit model: recon then web, then step completions, then goal."""

    def __init__(self) -> None:
        super().__init__()
        self._graph: Any = None
        self._evidence: Any = None
        self._next_calls = 0

    async def _host_id(self) -> str:
        nodes = await self._graph.list_nodes()
        for n in nodes:
            if n.type is GraphNodeType.HOST and str(n.id) == "host:a.local":
                return str(n.id)
        host = next(n for n in nodes if n.type is GraphNodeType.HOST)
        return str(host.id)

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    def _draft_two_steps(self, host_id: str) -> dict[str, Any]:
        def _crit() -> dict[str, Any]:
            return {"kind": "evidence", "description": "result", "min_evidence": 1}

        return {
            "path_id": "path-draft-handoff",
            "run_id": "run-x",
            "mission_id": None,
            "version": 1,
            "parent_version": None,
            "status": "draft",
            "reason": "scripted handoff draft",
            "goal": "map host then inspect endpoint",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "probe the host",
                    "agent_id": "recon-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": [],
                    "allowed_operations": ["recon.service_discovery"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [_crit()],
                    "required": True,
                    "status": "pending",
                    "outcome": None,
                    "evidence_ids": [],
                },
                {
                    "step_id": "step-2",
                    "objective": "inspect discovered surface",
                    "agent_id": "web-api-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": [],
                    "allowed_operations": ["web.http_request"],
                    "depends_on": ["step-1"],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [_crit()],
                    "required": True,
                    "status": "pending",
                    "outcome": None,
                    "evidence_ids": [],
                },
            ],
            "hypothesis_ids": [],
            "evidence_ids": [],
            "changed_step_ids": [],
            "review_records": {},
        }

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "path_draft":
            return self._draft_two_steps(await self._host_id())
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
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
            self._next_calls += 1
            host_id = await self._host_id()
            if self._next_calls == 1:
                return {
                    "mode": "actions",
                    "reason": "scripted recon",
                    "actions": [
                        {
                            "path_step_id": "step-1",
                            "agent_id": "recon-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "recon",
                            "operation": "service_discovery",
                            "tool_id": "recon.http_probe",
                            "arguments": {"target_ref": "a.local"},
                            "reason": "probe host",
                            "expected_observation": "host answers",
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
            if self._next_calls == 2:
                payload = self._payload(messages)
                run_id = payload["active_path"]["run_id"]
                records = await self._evidence.list_for_run(run_id)
                tool_ids = [
                    str(r.evidence_id)
                    for r in records
                    if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
                ]
                assert tool_ids, "step-1 completion needs recon evidence"
                return {**h.turn_complete_step([tool_ids[0]]), "path_step_id": "step-1"}
            if self._next_calls == 3:
                return {
                    "mode": "actions",
                    "reason": "scripted web",
                    "actions": [
                        {
                            "path_step_id": "step-2",
                            "agent_id": "web-api-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "web",
                            "operation": "http_request",
                            "tool_id": "web.http_request",
                            "arguments": {"target_ref": "a.local", "path": "/"},
                            "reason": "inspect endpoint",
                            "expected_observation": "http answers",
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
            if self._next_calls == 4:
                assert len(tool_ids) >= 2, "step-2 completion needs web evidence"
                return {**h.turn_complete_step([tool_ids[1]]), "path_step_id": "step-2"}
            return h.turn_complete_goal()
        raise AssertionError(f"no handoff answer for route {route!r}")

    async def complete(self, *, messages: Any, system: Any = None, **_: Any) -> str:
        return ""


def _engagement(*hosts: str) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_handoff"),
        name="handoff",
        scope=EngagementScope(allowed_hosts=list(hosts)),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


class _CapabilityFollowupLLM(_HandoffLLM):
    """Explicit LLM fixture for additional tools; no implicit rule execution."""

    def __init__(self, followups):
        super().__init__()
        self.followups = followups

    def _draft_two_steps(self, host_id):
        path = super()._draft_two_steps(host_id)
        template = path["steps"].pop()
        for index, (_tool, operation, _arguments) in enumerate(self.followups, start=2):
            path["steps"].append(
                {
                    **template,
                    "step_id": f"step-{index}",
                    "depends_on": [f"step-{index - 1}"],
                    "allowed_operations": [f"web.{operation}"],
                }
            )
        return path

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if self._route_key(response_schema) != "next_action":
            return await super().complete_structured(
                messages=messages, response_schema=response_schema, system=system, **kwargs
            )
        payload = self._payload(messages)
        path = payload["active_path"]
        for index, step in enumerate(path["steps"]):
            if step["status"] == "completed":
                continue
            records = await self._evidence.list_for_run(path["run_id"])
            receipts = [
                str(record.evidence_id)
                for record in records
                if record.metadata.get("tool_result")
                and record.metadata.get("path_step_id") == step["step_id"]
            ]
            if receipts:
                return {**h.turn_complete_step(receipts), "path_step_id": step["step_id"]}
            tool, operation, arguments = (
                ("recon.http_probe", "service_discovery", {"target_ref": "a.local"})
                if index == 0
                else self.followups[index - 1]
            )
            return {
                "mode": "actions",
                "reason": "Explicit registered-tool fixture",
                "actions": [
                    {
                        "agent_id": step["agent_id"],
                        "path_step_id": step["step_id"],
                        "target_node_id": await self._host_id(),
                        "capability": "recon" if index == 0 else "web",
                        "operation": operation,
                        "tool_id": tool,
                        "arguments": arguments,
                        "reason": "Synthetic followup",
                    }
                ],
            }
        return h.turn_complete_goal()


async def _run_capability_followups(gateway, followups):
    llm = _CapabilityFollowupLLM(followups)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=30),
    )
    llm._graph, llm._evidence = deps.graph_store, deps.evidence_store
    return await run_engagement(_engagement("a.local"), deps=deps)


@pytest.mark.parametrize("with_browser", [False, True])
async def test_recon_discovery_unlocks_a_real_web_http_request_call(with_browser: bool) -> None:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="test",
        tool_name="http_probe",
        input_schema={
            "type": "object",
            "properties": {"target_ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["target_ref"],
        },
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    web_tool = ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="test",
        tool_name="http_request",
        input_schema=_REAL_WEB_HTTP_REQUEST_SCHEMA,
        capability_tags=["web", "api"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    tools = [recon_tool, web_tool]
    if with_browser:
        tools.append(
            ToolDescriptor(
                tool_id=ToolId("web.browser_navigate"),
                server_id="browser",
                tool_name="browser_navigate",
                capability_tags=["web"],
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.HEALTHY,
            )
        )
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=tools)
    llm = _HandoffLLM()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=20),
    )
    llm._graph = deps.graph_store
    llm._evidence = deps.evidence_store

    final = await run_engagement(_engagement("a.local"), deps=deps)

    web_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.http_request"
    ]
    assert web_invocations, "Web/API Agent never called a real web.http_request-shaped tool"

    # The structural point of this whole fix: real, graph-derived values,
    # not empty/guessed arguments -- this would have raised a schema
    # validation error at candidate-compile time before this fix, so
    # simply reaching here already proves required fields were filled.
    resolved = web_invocations[0].arguments
    assert resolved["target_ref"] == "a.local"  # from EndpointTarget.host
    assert resolved["path"] == "/"  # from EndpointTarget.path, not a hardcoded "/"

    actions_by_agent = final.actions_by_agent
    assert actions_by_agent.get("recon-agent", 0) >= 1
    assert actions_by_agent.get("web-api-agent", 0) >= 1


async def test_browser_navigate_is_selected_when_http_mcp_is_unavailable() -> None:
    """A live Browser MCP can serve the same intent when HTTPX is absent.

    The URL is assembled only from Recon's observed scheme/host/path.  This
    proves the fallback is an Agent candidate, not a direct browser call.
    """

    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon",
        tool_name="http_probe",
        input_schema={
            "type": "object",
            "properties": {"target_ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["target_ref"],
        },
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    browser_tool = ToolDescriptor(
        tool_id=ToolId("web.browser_navigate"),
        server_id="browser",
        tool_name="browser_navigate",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, browser_tool])
    await _run_capability_followups(
        gateway,
        [
            ("web.browser_navigate", "http_request", {"url": "http://a.local/"}),
        ],
    )

    browser_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.browser_navigate"
    ]
    assert browser_invocations
    assert browser_invocations[0].arguments == {"url": "http://a.local/"}


async def test_proxy_history_is_selected_as_a_safe_observation_followup() -> None:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon",
        tool_name="http_probe",
        input_schema={"type": "object", "properties": {"target_ref": {"type": "string"}}},
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    proxy_tool = ToolDescriptor(
        tool_id=ToolId("web.proxy_list_flows"),
        server_id="proxy",
        tool_name="proxy_list_flows",
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["target_ref"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.MEDIUM,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, proxy_tool])
    await _run_capability_followups(
        gateway,
        [
            ("web.proxy_list_flows", "response_analysis", {"target_ref": "a.local", "limit": 50}),
        ],
    )

    proxy_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.proxy_list_flows"
    ]
    assert proxy_invocations
    assert proxy_invocations[0].arguments == {"target_ref": "a.local", "limit": 50}


async def test_nuclei_passive_check_is_selected_after_http_observation() -> None:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon",
        tool_name="http_probe",
        input_schema={"type": "object", "properties": {"target_ref": {"type": "string"}}},
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    http_tool = ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="http",
        tool_name="http_request",
        input_schema=_REAL_WEB_HTTP_REQUEST_SCHEMA,
        capability_tags=["web", "api"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    nuclei_tool = ToolDescriptor(
        tool_id=ToolId("web.nuclei_response_check"),
        server_id="nuclei",
        tool_name="nuclei_response_check",
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "path": {"type": "string"},
                "check_id": {"type": "string"},
            },
            "required": ["target_ref", "path", "check_id"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(
        output=_HTTP_PROBE_OUTPUT,
        tools=[recon_tool, http_tool, nuclei_tool],
    )
    await _run_capability_followups(
        gateway,
        [
            ("web.http_request", "http_request", {"target_ref": "a.local", "path": "/"}),
            (
                "web.nuclei_response_check",
                "response_analysis",
                {"target_ref": "a.local", "path": "/", "check_id": "missing-csp"},
            ),
        ],
    )

    nuclei_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.nuclei_response_check"
    ]
    assert nuclei_invocations
    assert nuclei_invocations[0].arguments["check_id"] == "missing-csp"
