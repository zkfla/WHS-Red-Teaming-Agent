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


async def test_recon_discovery_unlocks_a_real_web_http_request_call() -> None:
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
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, web_tool])
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
