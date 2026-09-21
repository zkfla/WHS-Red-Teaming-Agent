"""Real MCP schemas, changing responses and evidence-backed observation boundaries."""

from datetime import datetime, timezone

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
from redteam.orchestration.analysis import VerificationAssessment
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, build_test_application


class ChangingResponses(DummyToolGateway):
    def __init__(self, status, header):
        super().__init__()
        self.status = status
        self.header = header

    async def invoke(self, invocation):
        result = await super().invoke(invocation)
        is_web = invocation.tool_id == "web.http_request"
        output = {"status_code": self.status if is_web else 200, "redirect": False}
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
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=catalog,
        verifier=OverconfidentVerifier(),
        settings=OrchestrationSettings(max_steps=20, max_actions_per_turn=fanout),
    )
    final = await run_engagement(engagement, deps=deps)
    assert [i.tool_id for i in gateway.invocations] == ["recon.http_probe", "web.http_request"]
    assert all(i.arguments["target_ref"] == "bank-lab" for i in gateway.invocations)
    assert all("message" not in i.arguments for i in gateway.invocations)
    assert final.last_verdict is Verdict.UNKNOWN
    assert final.termination_reason == "no_actionable_plan"
    service = AttackGraphService(deps.graph_store)
    assert await service.list_findings() == []
    node = await service.get_node(endpoint_node_id("GET", "/", host="mock-bank"))
    assert node is not None
    assert attrs_of(node.properties)["status_code"] == status
    assert facts_of(node.properties)["assessment_kind"] == "observation"
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    evidence_ids = {str(e.evidence_id) for e in evidence}
    assert len(evidence_ids) == 2
    assert set(facts_of(node.properties)["evidence_ids"]) == evidence_ids
    assert {p["evidence_id"] for p in node.properties["provenance"]} == evidence_ids
    assert all(
        edge.properties["provenance"][0]["evidence_id"] in evidence_ids
        for edge in await service.list_edges()
        if edge.type.value in {"hosts", "exposes"}
    )
    observed = deps.event_bus.history(event_type="ObservationRecorded")
    assert len(observed) == 1
    assert observed[0].payload["evidence_id"] in evidence_ids
