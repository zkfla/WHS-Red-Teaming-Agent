from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, ToolHealthStatus
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.events import AGENT_CONTEXT_PREPARED
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import RuntimeState
from tests.fakes import build_test_application

ASSET = "workspace-test"
RUN_ID = RunId("run-analysis-activation")


def engagement(*, apk_ref: str | None = None, execution: bool = False) -> Engagement:
    metadata: dict = {"asset_ref": ASSET}
    if apk_ref is not None:
        metadata["apk_ref"] = apk_ref
    if execution:
        metadata["execution_agent"] = {
            "enabled": True,
            "assets": {ASSET: {"recipe": "line_count", "path": "input.txt"}},
        }
    return Engagement(
        engagement_id=EngagementId("eng-analysis-activation"),
        name="analysis activation guard",
        scope=EngagementScope(allowed_assets=[ASSET]),
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        metadata=metadata,
        created_at=datetime.now(timezone.utc),
    )


def descriptor(
    tool_id: str,
    *,
    operation: str,
    health: ToolHealthStatus = ToolHealthStatus.HEALTHY,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(tool_id),
        server_id="activation-test",
        tool_name=tool_id.rpartition(".")[2],
        input_schema={"type": "object", "properties": {}},
        operations=[operation],
        target_types=["asset"],
        capability_tags=["analysis"],
        health_status=health,
    )


async def prepared_context(
    configured: Engagement,
    tools: list[ToolDescriptor],
) -> tuple[
    dict,
    list[dict],
    list[GraphNode],
    InvestigationEngine,
    RuntimeState,
    RuntimeDeps,
    MCPToolRegistry,
]:
    catalog = MCPToolRegistry()
    for tool in tools:
        catalog.register(tool)
    deps = replace(
        build_test_application(tool_catalog=MCPToolCatalog(catalog)),
        engagement=configured,
    )
    service = AttackGraphService(deps.graph_store)
    await seed_from_engagement(service, configured, run_id=RUN_ID)
    observation = await service.observe(step=1)
    runtime = RuntimeState(
        run_id=RUN_ID,
        engagement_id=configured.engagement_id,
        current_goal="process the explicitly configured asset",
    )
    engine = InvestigationEngine(deps)
    context = await engine.context(runtime, observation)
    references = await engine.reference_paths(observation, service.view(), runtime)
    assets = await service.query(node_type=GraphNodeType.ASSET)
    return context, references, assets, engine, runtime, deps, catalog


def role_ids(context: dict) -> set[str]:
    return {item["agent_id"] for item in context["roles"]}


async def test_workspace_asset_does_not_activate_static_analysis() -> None:
    context, references, assets, _, _, _, _ = await prepared_context(
        engagement(),
        [descriptor("analysis.decode_apk", operation="static_analysis")],
    )

    facts = facts_of(assets[0].properties)
    assert facts["asset_ref"] == ASSET
    assert facts["target_ref"] == ASSET
    assert "apk_ref" not in facts
    assert "analysis-agent" not in role_ids(context)
    assert context["role_activation"]["analysis-agent"]["reason"] == "no_explicit_apk_asset"
    assert all(item["operation"] != "analysis.static_analysis" for item in references)


async def test_apk_without_healthy_static_tool_records_inactive_reason() -> None:
    context, references, _, _, _, _, _ = await prepared_context(
        engagement(apk_ref="insecurebankv2"),
        [
            descriptor(
                "analysis.decode_apk",
                operation="static_analysis",
                health=ToolHealthStatus.UNHEALTHY,
            )
        ],
    )

    assert "analysis-agent" not in role_ids(context)
    assert context["role_activation"]["analysis-agent"]["reason"] == (
        "no_healthy_static_analysis_tool"
    )
    assert all(item["operation"] != "analysis.static_analysis" for item in references)


async def test_explicit_apk_and_healthy_static_tool_activate_analysis() -> None:
    context, references, assets, _, _, _, _ = await prepared_context(
        engagement(apk_ref="insecurebankv2"),
        [descriptor("analysis.decode_apk", operation="static_analysis")],
    )

    assert facts_of(assets[0].properties)["apk_ref"] == "insecurebankv2"
    assert "analysis-agent" in role_ids(context)
    assert context["role_activation"]["analysis-agent"]["reason"] == "active"
    assert any(item["operation"] == "analysis.static_analysis" for item in references)


async def test_execution_workspace_exposes_only_command_recipe_without_mobile_analysis() -> None:
    context, references, _, _, _, _, _ = await prepared_context(
        engagement(execution=True),
        [descriptor("analysis.run_command", operation="command_run")],
    )

    assert context["role_activation"]["analysis-agent"]["reason"] == "no_explicit_apk_asset"
    assert "analysis-agent" in role_ids(context)
    mcp_cards = [item for item in context["tool_cards"] if item["kind"] == "mcp_tool"]
    assert [item["tool_name"] for item in mcp_cards] == ["run_command"]
    assert len(context["execution_options"]) == 1
    assert context["execution_options"][0]["operation"] == "command_run"
    assert all(
        item["operation"] not in {"analysis.static_analysis", "mobile.app_launch"}
        for item in references
    )


async def test_health_transition_keeps_each_activation_reason_in_event_history() -> None:
    tool = descriptor(
        "analysis.decode_apk",
        operation="static_analysis",
        health=ToolHealthStatus.UNHEALTHY,
    )
    context, _, _, engine, runtime, deps, registry = await prepared_context(
        engagement(apk_ref="insecurebankv2"),
        [tool],
    )
    engine._event_runtime = runtime
    await engine._ask(
        stage="activation_probe",
        payload=context,
        response_schema={"type": "object"},
    )

    assert registry.update_server_health(tool.server_id, ToolHealthStatus.HEALTHY)
    service = AttackGraphService(deps.graph_store)
    recovered = await engine.context(runtime, await service.observe(step=2))
    await engine._ask(
        stage="activation_probe",
        payload=recovered,
        response_schema={"type": "object"},
    )

    assert isinstance(deps.event_bus, InMemoryEventBus)
    reasons = [
        event.payload["role_activation"]["analysis-agent"]["reason"]
        for event in deps.event_bus.history()
        if event.event_type == AGENT_CONTEXT_PREPARED
    ]
    assert reasons == ["no_healthy_static_analysis_tool", "active"]
