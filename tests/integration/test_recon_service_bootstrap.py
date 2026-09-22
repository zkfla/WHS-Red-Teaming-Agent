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

from datetime import datetime, timezone

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import host_node_id, service_node_id
from redteam.attack_graph.service import AttackGraphService
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
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
    )
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
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
    )
    graph = AttackGraphService(deps.graph_store)
    await _seed_stalled_service(graph)

    await run_engagement(_engagement("a.local"), deps=deps)

    bootstraps = [
        item
        for item in gateway.invocations
        if str(item.tool_id) == "recon.http_probe"
        and item.arguments.get("path") == "/"
    ]
    assert len(bootstraps) == 1
