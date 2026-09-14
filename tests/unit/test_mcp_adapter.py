"""MCP Adapter Test: the dummy adapter satisfies the MCPAdapter/ToolGateway contract."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


async def test_example_adapter_discovers_and_invokes() -> None:
    adapter = ExampleMCPAdapter()

    tools = await adapter.discover_tools()
    assert len(tools) == 1

    result = await adapter.invoke(tools[0], {"message": "hi"})
    assert result.success is True
    assert isinstance(result.output, dict)
    assert result.output["echo"] == {"message": "hi"}

    assert await adapter.health_check() == ToolHealthStatus.HEALTHY


async def test_gateway_routes_invocation_to_registered_adapter() -> None:
    adapter = ExampleMCPAdapter()
    registry = MCPToolRegistry()
    for descriptor in await adapter.discover_tools():
        registry.register(descriptor)
    gateway = MCPToolGateway(registry, {adapter.server_id: adapter})

    tool_id = registry.all()[0].tool_id
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        tool_id=tool_id,
        operation="ping",
        arguments={"message": "hi"},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_1"), run_id=RunId("run_1")
        ),
        created_at=datetime.now(timezone.utc),
    )

    result = await gateway.invoke(invocation)
    assert result.success is True
    assert result.invocation_id == InvocationId("inv_1")
    assert result.action_id == ActionId("action_1")


async def test_gateway_returns_error_for_unknown_tool() -> None:
    gateway = MCPToolGateway(MCPToolRegistry(), {})
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        tool_id=ToolId("does.not.exist"),
        operation="ping",
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_1"), run_id=RunId("run_1")
        ),
        created_at=datetime.now(timezone.utc),
    )

    result = await gateway.invoke(invocation)
    assert result.success is False
    assert result.action_id == ActionId("action_1")
    assert result.error is not None
    assert result.error.code == "unknown_tool"
