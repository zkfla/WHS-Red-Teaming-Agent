from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.execution_bridge import (
    agent_tool_visible,
    bind_execution_arguments,
    execution_agent_options,
)
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import RuntimeState
from tests.fakes import build_test_application


def engagement(
    *, execution_agent: dict | None = None, apk_ref: str | None = None
) -> Engagement:
    metadata: dict[str, Any] = {"asset_ref": "workspace-test"}
    if apk_ref is not None:
        metadata["apk_ref"] = apk_ref
    if execution_agent is not None:
        metadata["execution_agent"] = execution_agent
    return Engagement(
        engagement_id=EngagementId("eng-test"),
        name="execution bridge",
        scope=EngagementScope(allowed_assets=["workspace-test"]),
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata=metadata,
    )


def execution_descriptor(
    name: str = "run_command", operation: str = "command_run"
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(f"analysis.{name}"),
        server_id="execution",
        tool_name=name,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        operations=[operation],
        target_types=["asset"],
        capability_tags=["analysis"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )


def analysis_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("analysis.decode_apk"),
        server_id="apktool",
        tool_name="decode_apk",
        input_schema={"type": "object"},
        operations=["static_analysis"],
        target_types=["asset"],
        capability_tags=["analysis"],
        health_status=ToolHealthStatus.HEALTHY,
    )


def catalog(*descriptors: ToolDescriptor) -> MCPToolGateway:
    registry = MCPToolRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    return MCPToolGateway(
        registry,
        {},
        {str(item.tool_id): tuple(item.operations) for item in descriptors},
        health_refresh_enabled=False,
    )


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside", "dir/../outside", "dir\\file", "dir//file"],
)
async def test_invalid_execution_workspace_path_is_rejected_before_graph_seed(
    path: str,
) -> None:
    configured = engagement(
        execution_agent={
            "enabled": True,
            "assets": {"workspace-test": {"recipe": "line_count", "path": path}},
        }
    )

    with pytest.raises(ValueError, match="relative POSIX path"):
        await seed_from_engagement(
            AttackGraphService(InMemoryAttackGraphStore()), configured
        )


async def test_enabled_bridge_uses_seeded_facts_and_overwrites_model_command() -> None:
    configured = engagement(
        execution_agent={
            "enabled": True,
            "assets": {
                "workspace-test": {"recipe": "line_count", "path": "input.txt"}
            },
        }
    )
    service = AttackGraphService(InMemoryAttackGraphStore())
    [node] = await seed_from_engagement(service, configured, run_id=RunId("run-test"))

    assert facts_of(node.properties) | {
        "execution_recipe": "line_count",
        "execution_workspace_path": "input.txt",
    } == facts_of(node.properties)
    options = await execution_agent_options(service, configured, RunId("run-test"))
    assert options == [
        {
            "target_node_id": "asset:workspace-test",
            "capability": "analysis",
            "operation": "command_run",
            "tool_id": "analysis.run_command",
            "arguments": {
                "asset_ref": "workspace-test",
                "executable": "wc",
                "args": ["-l", "input.txt"],
                "timeout_seconds": 10.0,
                "engagement_id": "eng-test",
                "run_id": "run-test",
            },
        }
    ]
    bound = bind_execution_arguments(
        engagement=configured,
        run_id=RunId("run-test"),
        node=node,
        tool=execution_descriptor(),
        capability="analysis",
        operation="command_run",
        arguments={"executable": "python", "args": ["-c", "malicious"]},
    )
    assert bound == options[0]["arguments"]


def test_bridge_exposes_only_synchronous_fixed_execution_operation() -> None:
    assert not agent_tool_visible(execution_descriptor(), execution_ready=False)
    assert agent_tool_visible(execution_descriptor(), execution_ready=True)
    assert not agent_tool_visible(
        execution_descriptor("job_start", "command_job_start"),
        execution_ready=True,
    )
    assert agent_tool_visible(analysis_descriptor(), execution_ready=False)


async def test_legacy_optout_hides_execution_options_but_registered_direct_tool_visible() -> None:
    configured = engagement(apk_ref="insecurebankv2")
    graph_store = InMemoryAttackGraphStore()
    service = AttackGraphService(graph_store)
    await seed_from_engagement(service, configured, run_id=RunId("run-test"))
    baseline_catalog = catalog(analysis_descriptor())
    extended_catalog = catalog(analysis_descriptor(), execution_descriptor())
    baseline = build_test_application(graph_store=graph_store, tool_catalog=baseline_catalog)
    baseline = replace(baseline, engagement=configured)
    extended = replace(baseline, tool_catalog=extended_catalog)
    runtime = RuntimeState(
        run_id=RunId("run-test"),
        engagement_id=configured.engagement_id,
        current_goal="inspect the asset",
        remaining_budget=4,
    )
    observation = GraphObservation(
        node_counts={GraphNodeType.ASSET.value: 1},
        nodes_by_type={GraphNodeType.ASSET.value: ["workspace-test"]},
        node_ids_by_type={GraphNodeType.ASSET.value: ["asset:workspace-test"]},
    )

    without_execution = await InvestigationEngine(baseline).context(runtime, observation)
    disabled_execution = await InvestigationEngine(extended).context(runtime, observation)

    # Legacy channel: opt-in off means no fixed-recipe options in either context.
    assert "execution_options" not in disabled_execution
    assert "execution_options" not in without_execution
    # New channel: a registered direct tool is visible in the new context.
    # Cards briefing: only the MCP-kind slice mirrors the old full dumps;
    # locals/programs/references list alongside and are not asserted here.
    assert [
        item["tool_id"]
        for item in without_execution["tool_cards"]
        if item["kind"] == "mcp_tool"
    ] == ["analysis.decode_apk"]
    assert sorted(
        item["tool_id"]
        for item in disabled_execution["tool_cards"]
        if item["kind"] == "mcp_tool"
    ) == ["analysis.decode_apk", "analysis.run_command"]
    # Visibility aside, no other context field may drift with registration.
    # The card briefing keys (shallow, no schemas) are the visibility surface.
    steady = (
        "tools_by_role",
        "tool_cards",
        "tool_cards_total",
        "tool_cards_truncated",
        "tool_cards_counts",
        "tool_cards_guidance",
        "program_manifest_status",
        "workroom_status",
    )
    assert {k: v for k, v in disabled_execution.items() if k not in steady} == {
        k: v for k, v in without_execution.items() if k not in steady
    }
    for context in (without_execution, disabled_execution):
        mapping = context["tools_by_role"]
        role_ids = {item["agent_id"] for item in context["roles"]}
        assert set(mapping) == role_ids
        tool_ids = {
            item["tool_id"] for item in context["tool_cards"] if item["kind"] == "mcp_tool"
        }
        union_ids = {tool_id for ids in mapping.values() for tool_id in ids}
        assert union_ids == tool_ids
    assert {
        tool_id for ids in without_execution["tools_by_role"].values() for tool_id in ids
    } == {"analysis.decode_apk"}
    assert {
        tool_id for ids in disabled_execution["tools_by_role"].values() for tool_id in ids
    } == {"analysis.decode_apk", "analysis.run_command"}
    assert all(
        "analysis.run_command" not in ids
        for ids in without_execution["tools_by_role"].values()
    )
    assert sorted(disabled_execution["tools_by_role"]["analysis-agent"]) == [
        "analysis.decode_apk",
        "analysis.run_command",
    ]
    assert without_execution["tools_by_role"]["analysis-agent"] == [
        "analysis.decode_apk"
    ]
    for role_id in ("recon-agent", "web-api-agent", "mobile-agent"):
        assert "analysis.run_command" not in disabled_execution["tools_by_role"][role_id]
        assert "analysis.run_command" not in without_execution["tools_by_role"][role_id]


async def test_custom_selections_map_per_role_in_live_context() -> None:
    configured = engagement(apk_ref="insecurebankv2")
    graph_store = InMemoryAttackGraphStore()
    service = AttackGraphService(graph_store)
    await seed_from_engagement(service, configured, run_id=RunId("run-test"))
    agents = AgentRegistry(
        definitions=[
            AgentDefinition(
                agent_id=AgentId("custom-a"),
                role="custom",
                name="Custom A",
                provider_id="playbook:analysis",
                context_profile=ContextProfile(profile_id="custom-a"),
                capabilities=["analysis"],
                tool_selection={"server_ids": ["apktool"]},
            ),
            AgentDefinition(
                agent_id=AgentId("custom-b"),
                role="custom",
                name="Custom B",
                provider_id="playbook:analysis",
                context_profile=ContextProfile(profile_id="custom-b"),
                capabilities=["analysis"],
                tool_selection={"server_ids": ["execution"]},
            ),
        ],
        specs=[
            AgentSpec(agent_id=AgentId("custom-a"), name="Custom A", capabilities=["analysis"]),
            AgentSpec(agent_id=AgentId("custom-b"), name="Custom B", capabilities=["analysis"]),
        ],
    )
    deps = build_test_application(
        graph_store=graph_store,
        tool_catalog=catalog(analysis_descriptor(), execution_descriptor()),
        agents=agents,
    )
    deps = replace(deps, engagement=configured)
    runtime = RuntimeState(
        run_id=RunId("run-test"),
        engagement_id=configured.engagement_id,
        current_goal="inspect the asset",
        remaining_budget=4,
    )
    observation = GraphObservation(
        node_counts={GraphNodeType.ASSET.value: 1},
        nodes_by_type={GraphNodeType.ASSET.value: ["workspace-test"]},
        node_ids_by_type={GraphNodeType.ASSET.value: ["asset:workspace-test"]},
    )
    context = await InvestigationEngine(deps).context(runtime, observation)
    assert {item["agent_id"] for item in context["roles"]} == {"custom-a", "custom-b"}
    assert set(context["tools_by_role"]) == {"custom-a", "custom-b"}
    assert context["tools_by_role"]["custom-a"] == ["analysis.decode_apk"]
    assert context["tools_by_role"]["custom-b"] == ["analysis.run_command"]
    assert "analysis.run_command" not in context["tools_by_role"]["custom-a"]
    assert "analysis.decode_apk" not in context["tools_by_role"]["custom-b"]
    assert {
        item["tool_id"] for item in context["tool_cards"] if item["kind"] == "mcp_tool"
    } == {"analysis.decode_apk", "analysis.run_command"}
    assert {tool_id for ids in context["tools_by_role"].values() for tool_id in ids} == {
        item["tool_id"] for item in context["tool_cards"] if item["kind"] == "mcp_tool"
    }


async def test_catalog_none_leaves_empty_mapping_per_visible_role() -> None:
    configured = engagement(apk_ref="insecurebankv2")
    graph_store = InMemoryAttackGraphStore()
    service = AttackGraphService(graph_store)
    await seed_from_engagement(service, configured, run_id=RunId("run-test"))
    deps = build_test_application(
        graph_store=graph_store, tool_catalog=catalog(analysis_descriptor())
    )
    deps = replace(deps, engagement=configured, tool_catalog=None)
    runtime = RuntimeState(
        run_id=RunId("run-test"),
        engagement_id=configured.engagement_id,
        current_goal="inspect the asset",
        remaining_budget=4,
    )
    observation = GraphObservation(
        node_counts={GraphNodeType.ASSET.value: 1},
        nodes_by_type={GraphNodeType.ASSET.value: ["workspace-test"]},
        node_ids_by_type={GraphNodeType.ASSET.value: ["asset:workspace-test"]},
    )
    context = await InvestigationEngine(deps).context(runtime, observation)
    assert [
        item for item in context["tool_cards"] if item["kind"] == "mcp_tool"
    ] == []
    assert context["roles"] != []
    assert set(context["tools_by_role"]) == {
        item["agent_id"] for item in context["roles"]
    }
    assert all(ids == [] for ids in context["tools_by_role"].values())
