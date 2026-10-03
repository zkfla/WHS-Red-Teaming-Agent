"""C profiles: actual MCP discovery -> scripted Agent -> Control -> Gateway.

Both upstream network providers are synthetic; no financial or paid API access.
"""

import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.servers.page_text import PageTextConfig
from redteam.capability.servers.page_text_mcp import create_server as page_server
from redteam.capability.servers.web_search import SearchConfig
from redteam.capability.servers.web_search_mcp import create_server as search_server
from redteam.capability.settings import MCPServerSettings
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import UrlTarget
from redteam.core.convert import NonAllowDecisionError, build_tool_invocation
from redteam.core.enums import EvidenceSourceType, GraphNodeType, PolicyVerdict, RiskLevel
from redteam.core.ids import ActionId, AgentId, EngagementId, GraphNodeId, RunId
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.llm_provider import LLMCandidateProvider
from redteam.orchestration.agents.providers import GraphCandidateContext
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from tests.fakes import build_test_application
from tests.integration._engine_llm_harness import ScriptedLLM, review_accept
from tests.integration.test_execution_agent_e2e import draft_path, hold, turn_action
from tests.integration.test_page_text_mcp import (
    InProcessTransport,
    _reader,
)
from tests.integration.test_page_text_mcp import (
    _config_file as page_config,
)
from tests.integration.test_page_text_mcp import (
    _settings as page_settings,
)
from tests.integration.test_web_search_mcp import (
    KEY,
    QUERY,
    FakeProvider,
)
from tests.integration.test_web_search_mcp import (
    _config_file as search_config,
)
from tests.integration.test_web_search_mcp import (
    _settings as search_settings,
)
from tests.unit.test_llm_candidate_provider import _ScriptedLLM


async def configured_gateway(tmp_path, monkeypatch):
    page_calls = []
    search_provider = FakeProvider()

    def page_response(request):
        page_calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=httpx.ByteStream(b"Synthetic approved reference"),
        )

    page_settings(monkeypatch, page_config(tmp_path / "page.json"))
    search_settings(monkeypatch, search_config(tmp_path / "search.json"))
    config = PageTextConfig(allowed_origins=("https://docs.example",))
    transports = {
        "page-text": InProcessTransport(
            page_server(config, reader=_reader(config, httpx.MockTransport(page_response)))
        ),
        "web-search": InProcessTransport(
            search_server(SearchConfig(external_search_enabled=True), provider=search_provider)
        ),
    }
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_transport",
        lambda settings: transports[settings.server_id],
    )
    gateway = await build_mcp_tool_gateway(list(transports), health_refresh_enabled=False)
    return gateway, page_calls, search_provider


@pytest.mark.parametrize("operation", ["page_read", "web_search"])
@pytest.mark.parametrize("permission", ["allow", "deny_operation", "deny_scope", "approval"])
async def test_c_profile_agent_pipeline_keeps_control_gate(
    operation, permission, tmp_path, monkeypatch
):
    gateway, page_calls, search_provider = await configured_gateway(tmp_path, monkeypatch)
    assert len(gateway.all()) == 2
    assert {x.operation for x in gateway.list_candidate_metadata()} == {"page_read", "web_search"}
    target = UrlTarget(url="https://docs.example/guide")
    node_id = node_id_for_target(target)
    graph = AttackGraphService(InMemoryAttackGraphStore())
    await graph.upsert_node(
        GraphNodeType.ENDPOINT, node_id=node_id, facts={"observed_target": target.url}
    )
    arguments = {} if operation == "page_read" else {"query": QUERY}
    action = {
        "capability": "web",
        "operation": operation,
        "target_node_id": str(node_id),
        "arguments": arguments,
    }
    spec = AgentSpec(agent_id=AgentId("web-api-agent"), name="web-api", capabilities=["web"])
    context = GraphCandidateContext(
        view=graph.view(),
        observation=await graph.observe(step=1),
        step=1,
        available_operations=(("web", "page_read"), ("web", "web_search")),
    )
    llm = _ScriptedLLM([action])
    batch = await LLMCandidateProvider(llm, gateway).propose_graph(spec, context)
    assert len(batch.candidates) == 1 and not batch.drops
    assert f"web / {operation}" in llm.last_prompt
    candidates, rejected = CandidateCompiler(gateway).compile_drafts_with_rejections(
        batch.candidates
    )
    assert len(candidates) == 1 and not rejected
    step = candidates[0].plan_step
    engagement = Engagement(
        engagement_id=EngagementId("eng-common-contract"),
        name="synthetic integration",
        scope=EngagementScope(
            allowed_hosts=["other.example" if permission == "deny_scope" else "docs.example"]
        ),
        rules_of_engagement=[
            RuleOfEngagement(
                name="explicit public reference intents",
                allowed_capabilities=["web"],
                allowed_operations=["web.page_read", "web.web_search"],
                denied_operations=[f"web.{operation}"] if permission == "deny_operation" else [],
            )
        ],
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=100),
        approval_policy=ApprovalPolicy(
            auto_approve_up_to=RiskLevel.LOW if permission != "approval" else None,
            require_approval_for=[f"web.{operation}"] if permission == "approval" else [],
        ),
        created_at=datetime.now(timezone.utc),
    )
    proposal = ActionProposal(
        action_id=ActionId("action-common"),
        run_id=RunId("run-common"),
        engagement_id=engagement.engagement_id,
        agent_id=spec.agent_id,
        capability=step.capability,
        operation=step.operation,
        target=step.target,
        arguments=step.arguments,
        reason=step.reason,
        created_at=datetime.now(timezone.utc),
    )
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement}, risk_resolver=RegistryRiskResolver(gateway)
    )
    decision = await control.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    if permission == "allow":
        assert decision.verdict == PolicyVerdict.ALLOW
        result = await gateway.invoke(
            build_tool_invocation(proposal, decision, tool_id=step.tool_id)
        )
        assert result.success and result.action_id == proposal.action_id
        assert result.tool_id == step.tool_id
        assert len(page_calls) == (1 if operation == "page_read" else 0)
        assert len(search_provider.calls) == (1 if operation == "web_search" else 0)
    else:
        expected = (
            PolicyVerdict.REQUIRE_APPROVAL if permission == "approval" else PolicyVerdict.DENY
        )
        assert decision.verdict == expected
        with pytest.raises(NonAllowDecisionError):
            build_tool_invocation(proposal, decision, tool_id=step.tool_id)
        assert not page_calls and not search_provider.calls


@pytest.mark.parametrize(
    "operation,transport",
    [
        ("page_read", "in_process"),
        ("web_search", "in_process"),
        ("web_search", "stdio"),
    ],
)
async def test_accepted_path_engine_executes_c_profile_and_records_evidence(
    operation,
    transport,
    tmp_path,
    monkeypatch,
):
    if transport == "stdio":
        path = search_config(tmp_path / "search.json")
        search_settings(monkeypatch, path)
        settings = MCPServerSettings.from_environment("web-search")
        fixture = Path(__file__).with_name("_web_search_stdio_fake.py")

        def fake_stdio(_):
            return StdioMCPTransport(
                sys.executable,
                args=(
                    str(fixture),
                    "--config",
                    str(path),
                    "--sha256",
                    settings.web_search_config_sha256 or "",
                ),
                env={"TAVILY_API_KEY": KEY},
            )

        monkeypatch.setattr("redteam.capability.bootstrap._create_transport", fake_stdio)
        gateway = await build_mcp_tool_gateway(["web-search"], health_refresh_enabled=False)
        page_calls, search_provider = [], FakeProvider()
    else:
        gateway, page_calls, search_provider = await configured_gateway(tmp_path, monkeypatch)
    run_id = RunId("run-test")
    target = UrlTarget(url="https://docs.example/guide")
    node_id = str(node_id_for_target(target))
    tool_id = "web.fetch_page_text" if operation == "page_read" else "web.web_search"
    configured = Engagement(
        engagement_id=EngagementId("eng-test"),
        name="public reference engine",
        scope=EngagementScope(allowed_hosts=["docs.example"]),
        rules_of_engagement=[
            RuleOfEngagement(
                name="explicit reference intent",
                allowed_capabilities=["web"],
                allowed_operations=[f"web.{operation}"],
            )
        ],
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(auto_approve_up_to=RiskLevel.LOW),
        created_at=datetime.now(timezone.utc),
    )
    path = draft_path()
    path["goal"] = "read approved public reference"
    path["steps"][0].update(
        agent_id="web-api-agent", target_node_ids=[node_id], allowed_operations=[f"web.{operation}"]
    )
    turn = turn_action()
    turn["actions"][0].update(
        agent_id="web-api-agent",
        target_node_id=node_id,
        capability="web",
        operation=operation,
        tool_id=tool_id,
        arguments={"url": target.url} if operation == "page_read" else {"query": QUERY},
    )
    llm = ScriptedLLM()
    llm.add("path_draft", path)
    llm.add("path_review", review_accept())
    llm.add("next_action", turn)
    llm.add("next_action", hold())
    control = DefaultControlPlane(
        {configured.engagement_id: configured}, risk_resolver=RegistryRiskResolver(gateway)
    )
    deps = replace(
        build_test_application(
            control_plane=control,
            tool_gateway=gateway,
            tool_catalog=gateway,
            llm_client=llm,
            settings=OrchestrationSettings(max_steps=12, checkpointing=False),
        ),
        engagement=configured,
    )
    graph = AttackGraphService(deps.graph_store)
    await graph.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=GraphNodeId(node_id),
        attrs={"in_scope": True},
        facts={"observed_target": target.url},
        provenance=Provenance(
            source="operator:synthetic-reference",
            run_id=str(run_id),
            confidence=Confidence.SERVER_DECLARED,
        ),
    )
    final = await run_engagement(configured, deps=deps, run_id=run_id)
    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD, "\n".join(
        call["messages"][0]["content"][-500:] for call in llm.calls
    )
    assert final.actions_executed == 1
    assert len(page_calls) == (1 if operation == "page_read" else 0)
    if transport == "in_process":
        assert len(search_provider.calls) == (1 if operation == "web_search" else 0)
    evidence = await deps.evidence_store.list_for_run(run_id)
    tool_evidence = [x for x in evidence if x.source_type is EvidenceSourceType.TOOL]
    assert len(tool_evidence) == 1 and tool_evidence[0].metadata["success"] is True
    prompt = json.loads(llm.calls[0]["messages"][0]["content"])
    shown = [tool for tool in prompt["tools"] if tool["tool_id"] == tool_id]
    assert len(shown) == 1 and shown[0]["operations"] == [operation]
    # Search result URLs are reference material, not new approved targets.
    if operation == "web_search":
        returned = UrlTarget(url="https://docs.example.org/reference")
        assert await graph.get_node(node_id_for_target(returned)) is None
        assert configured.scope.allowed_hosts == ["docs.example"]
    assert await graph.list_findings() == []
