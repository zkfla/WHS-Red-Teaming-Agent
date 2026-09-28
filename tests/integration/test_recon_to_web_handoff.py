"""End to end: one HOST in scope -> Recon runs -> Attack Graph gains a
SERVICE/ENDPOINT -> Web/API Agent activates -> its playbook resolves
real ``target_ref``/``path`` arguments from that ENDPOINT's own graph
context -> a real (schema-enforced) ``web.http_request``-shaped tool is
actually invoked.

Closes the structural gap where a run used to stop at
``no_actionable_plan`` right after Recon's single probe, first because
SERVICE/ENDPOINT were never created (fixed earlier), and then because
``WEB_API_AGENT``'s own playbook had empty ``arguments={}`` that could
never satisfy a real ``web.http_request`` tool's required
``target_ref``/``path`` (fixed here). The fake web tool below uses the
*real*, required-field schema
(``redteam.capability.servers.web_http.http_request``'s MCP-declared
schema, verified directly: ``required: ["target_ref", "path"]``) --
this test would fail if candidate compilation ever again produced
unresolved/guessed arguments.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, build_test_application

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
        tools.append(ToolDescriptor(
            tool_id=ToolId("web.browser_navigate"), server_id="browser",
            tool_name="browser_navigate", capability_tags=["web"],
            input_schema={"type": "object", "properties": {"url": {"type": "string"}},
                          "required": ["url"]},
            risk_level=RiskLevel.LOW, health_status=ToolHealthStatus.HEALTHY,
        ))
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=tools)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
    )

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
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
    )

    await run_engagement(_engagement("a.local"), deps=deps)

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
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
    )

    await run_engagement(_engagement("a.local"), deps=deps)

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
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
    )

    await run_engagement(_engagement("a.local"), deps=deps)

    nuclei_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.nuclei_response_check"
    ]
    assert nuclei_invocations
    assert nuclei_invocations[0].arguments["check_id"] == "missing-csp"
