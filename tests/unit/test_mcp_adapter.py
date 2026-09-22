"""MCP Adapter Test: the dummy adapter satisfies the MCPAdapter/ToolGateway contract."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter
from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.adapters.mcp.mobile import MobileMCPAdapter
from redteam.capability.adapters.mcp.naming import build_mcp_tool_id
from redteam.capability.adapters.mcp.recon import ReconMCPAdapter
from redteam.capability.adapters.mcp.transport import (
    AllowlistedMCPTransport,
    RemoteCallResult,
    RemoteTool,
)
from redteam.capability.adapters.mcp.web_api import WebAPIMCPAdapter
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry, ToolRegistrationConflictError
from redteam.core.contracts.tool import ExecutionContext, ToolDescriptor, ToolInvocation
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


class FakeTransport:
    def __init__(
        self,
        *,
        tools: list[RemoteTool] | None = None,
        result: RemoteCallResult | None = None,
        healthy: bool = True,
    ) -> None:
        self.tools = tools or []
        self.result = result or RemoteCallResult(is_error=False, output={"ok": True})
        self.healthy = healthy
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[RemoteTool]:
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        self.calls.append((tool_name, arguments))
        return self.result

    async def health_check(self) -> bool:
        return self.healthy


def _invocation(
    *,
    tool_id: ToolId | None = None,
    operation: str = "ping",
    timeout_seconds: float = 30.0,
) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        tool_id=tool_id or ToolId("recon.ping"),
        operation=operation,
        arguments={"message": "hi"},
        timeout_seconds=timeout_seconds,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_1"), run_id=RunId("run_1")
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_example_adapter_discovers_and_invokes() -> None:
    adapter = ExampleMCPAdapter()

    tools = await adapter.discover_tools()
    assert len(tools) == 1

    result = await adapter.invoke(tools[0], {"message": "hi"})
    assert result.success is True
    assert isinstance(result.output, dict)
    assert result.output["echo"] == {"message": "hi"}

    assert await adapter.health_check() == ToolHealthStatus.HEALTHY


async def test_adapter_preserves_remote_output_schema_in_registry_metadata() -> None:
    transport = FakeTransport(
        tools=[
            RemoteTool(
                name="http_probe",
                description="probe",
                input_schema={
                    "type": "object",
                    "properties": {"target_ref": {"type": "string"}},
                    "required": ["target_ref"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"status_code": {"type": "integer"}},
                    "required": ["status_code"],
                },
            )
        ]
    )
    tools = await ReconMCPAdapter(
        server_id="recon", transport=transport
    ).discover_tools()

    descriptor = tools[0]
    assert descriptor.required_arguments == ("target_ref",)
    assert descriptor.optional_arguments == ()
    assert descriptor.output_schema["required"] == ["status_code"]


async def test_gateway_rejects_output_that_breaks_registered_schema() -> None:
    adapter = ExampleMCPAdapter()
    registry = MCPToolRegistry()
    descriptor = (await adapter.discover_tools())[0].model_copy(
        update={"output_schema": {"type": "string"}}
    )
    registry.register(descriptor)
    result = await MCPToolGateway(registry, {adapter.server_id: adapter}).invoke(
        _invocation(operation="service_discovery")
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_output_schema_mismatch"


async def test_gateway_routes_invocation_to_registered_adapter() -> None:
    adapter = ExampleMCPAdapter()
    registry = MCPToolRegistry()
    for descriptor in await adapter.discover_tools():
        registry.register(descriptor)
    gateway = MCPToolGateway(registry, {adapter.server_id: adapter})

    tool_id = registry.all()[0].tool_id
    invocation = _invocation(tool_id=tool_id)

    result = await gateway.invoke(invocation)
    assert result.success is True
    assert result.invocation_id == InvocationId("inv_1")
    assert result.action_id == ActionId("action_1")


async def test_gateway_returns_error_for_unknown_tool() -> None:
    gateway = MCPToolGateway(MCPToolRegistry(), {})
    invocation = _invocation(tool_id=ToolId("does.not.exist"))

    result = await gateway.invoke(invocation)
    assert result.success is False
    assert result.action_id == ActionId("action_1")
    assert result.error is not None
    assert result.error.code == "unknown_tool"


async def test_web_api_adapter_discovers_and_invokes_independently() -> None:
    transport = FakeTransport(
        tools=[
            RemoteTool(
                name="Send HTTP Request",
                description="Send an authorized HTTP request",
                input_schema={"type": "object"},
            )
        ],
        result=RemoteCallResult(is_error=False, output={"status_code": 200}),
    )
    adapter = WebAPIMCPAdapter(
        server_id="web-server",
        endpoint="http://mcp.local/mcp",
        transport=transport,
    )

    tools = await adapter.discover_tools()
    assert tools[0].tool_id == ToolId("web.send_http_request")
    assert tools[0].capability_tags == ["web", "api"]
    assert tools[0].health_status == ToolHealthStatus.HEALTHY

    result = await adapter.invoke(tools[0], {"url": "http://target.local"})
    assert result.success is True
    assert result.output == {"status_code": 200}
    assert transport.calls == [("Send HTTP Request", {"url": "http://target.local"})]


async def test_recon_adapter_has_separate_namespace() -> None:
    transport = FakeTransport(
        tools=[RemoteTool(name="Probe HTTP", description="", input_schema={})]
    )
    adapter = ReconMCPAdapter(
        server_id="recon-server",
        endpoint="http://mcp.local/mcp",
        transport=transport,
    )

    tools = await adapter.discover_tools()
    assert tools[0].tool_id == ToolId("recon.probe_http")
    assert tools[0].capability_tags == ["recon"]


@pytest.mark.parametrize(
    ("adapter_type", "server_id", "remote_name", "expected_id", "expected_tags"),
    [
        (
            MobileMCPAdapter,
            "mobile-server",
            "Inspect APK",
            ToolId("mobile.inspect_apk"),
            ["mobile"],
        ),
        (
            AnalysisMCPAdapter,
            "analysis-server",
            "Compare Response",
            ToolId("analysis.compare_response"),
            ["analysis"],
        ),
    ],
)
async def test_mobile_and_analysis_adapters_have_independent_namespaces(
    adapter_type: type[MobileMCPAdapter] | type[AnalysisMCPAdapter],
    server_id: str,
    remote_name: str,
    expected_id: ToolId,
    expected_tags: list[str],
) -> None:
    transport = FakeTransport(tools=[RemoteTool(name=remote_name, description="", input_schema={})])
    adapter = adapter_type(server_id=server_id, transport=transport)

    tools = await adapter.discover_tools()

    assert tools[0].tool_id == expected_id
    assert tools[0].capability_tags == expected_tags


async def test_adapter_normalizes_remote_tool_error() -> None:
    transport = FakeTransport(
        tools=[RemoteTool(name="request", description="", input_schema={})],
        result=RemoteCallResult(is_error=True, output="request rejected"),
    )
    adapter = WebAPIMCPAdapter(
        server_id="web-server",
        endpoint="http://mcp.local/mcp",
        transport=transport,
    )
    tool = (await adapter.discover_tools())[0]

    result = await adapter.invoke(tool, {})
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "mcp_tool_error"
    assert result.error.message == "request rejected"


def test_tool_id_naming_is_deterministic() -> None:
    assert build_mcp_tool_id("Web API", "Send  HTTP/Request") == ToolId("web_api.send_http_request")


def test_registry_rejects_conflicting_tool_ids() -> None:
    registry = MCPToolRegistry()
    first = ToolDescriptor(
        tool_id=ToolId("web.request"),
        server_id="web-a",
        tool_name="request",
    )
    registry.register(first)

    with pytest.raises(ToolRegistrationConflictError):
        registry.register(first.model_copy(update={"server_id": "web-b"}))


def test_registry_replaces_one_servers_discovery_snapshot_atomically() -> None:
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.probe"),
            server_id="recon-server",
            tool_name="probe",
        )
    )
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("web.old"),
            server_id="web-server",
            tool_name="old",
        )
    )
    replacement = ToolDescriptor(
        tool_id=ToolId("web.request"),
        server_id="web-server",
        tool_name="request",
    )

    registry.replace_server_tools("web-server", [replacement])

    assert [tool.tool_id for tool in registry.all()] == [
        ToolId("recon.probe"),
        ToolId("web.request"),
    ]
    assert registry.get(ToolId("web.old")) is None


async def test_gateway_routes_tool_when_intent_differs_from_remote_tool_name() -> None:
    backend = FakeTransport(tools=[RemoteTool("http_request", "", {})])
    adapter = WebAPIMCPAdapter(server_id="http", transport=backend)
    registry = MCPToolRegistry()
    registry.replace_server_tools("http", await adapter.discover_tools())
    gateway = MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        {"web.http_request": ("http_request", "response_analysis")},
    )

    result = await gateway.invoke(
        _invocation(
            tool_id=ToolId("web.http_request"),
            operation="response_analysis",
        )
    )

    assert result.success is True
    assert backend.calls == [("http_request", {"message": "hi"})]
    assert [
        tool.tool_id for tool in gateway.list_tools_for_action("web", "response_analysis")
    ] == [ToolId("web.http_request")]


async def test_gateway_blocks_tool_substitution_outside_operation_binding() -> None:
    backend = FakeTransport(tools=[RemoteTool("proxy_mutate_json", "", {})])
    adapter = WebAPIMCPAdapter(server_id="proxy", transport=backend)
    registry = MCPToolRegistry()
    registry.replace_server_tools("proxy", await adapter.discover_tools())
    gateway = MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        {"web.proxy_mutate_json": ("parameter_fuzz",)},
    )

    result = await gateway.invoke(
        _invocation(
            tool_id=ToolId("web.proxy_mutate_json"),
            operation="response_analysis",
        )
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "operation_not_allowed_for_tool"
    assert backend.calls == []


class SlowAdapter(ExampleMCPAdapter):
    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        await asyncio.sleep(1)
        return await super().invoke(tool, arguments)


async def test_gateway_enforces_invocation_timeout() -> None:
    adapter = SlowAdapter()
    registry = MCPToolRegistry()
    registry.register((await adapter.discover_tools())[0])
    gateway = MCPToolGateway(registry, {adapter.server_id: adapter})

    result = await gateway.invoke(_invocation(timeout_seconds=0.001))

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_timeout"
    assert result.error.retryable is True


async def test_gateway_blocks_unhealthy_server_before_transport_call() -> None:
    backend = FakeTransport(tools=[RemoteTool("http_request", "", {})], healthy=False)
    adapter = WebAPIMCPAdapter(server_id="http", transport=backend)
    registry = MCPToolRegistry()
    registry.replace_server_tools("http", await adapter.discover_tools())
    gateway = MCPToolGateway(registry, {adapter.server_id: adapter})

    result = await gateway.invoke(
        _invocation(tool_id=ToolId("web.http_request"), operation="http_request")
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_unhealthy"
    assert result.error.retryable is True
    assert backend.calls == []


class ConcurrentAdapter(ExampleMCPAdapter):
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().invoke(tool, arguments)
        finally:
            self.active -= 1


async def test_gateway_limits_concurrency_per_mcp_server() -> None:
    adapter = ConcurrentAdapter()
    registry = MCPToolRegistry()
    registry.replace_server_tools(adapter.server_id, await adapter.discover_tools())
    gateway = MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        server_concurrency={adapter.server_id: 1},
    )

    first, second = await asyncio.gather(
        gateway.invoke(_invocation()),
        gateway.invoke(_invocation()),
    )

    assert first.success and second.success
    assert adapter.peak == 1


def test_gateway_rejects_concurrency_for_unknown_server() -> None:
    with pytest.raises(ValueError, match="unknown MCP servers"):
        MCPToolGateway(MCPToolRegistry(), {}, server_concurrency={"typo": 1})


async def test_gateway_omits_output_above_server_byte_limit() -> None:
    backend = FakeTransport(
        tools=[RemoteTool("http_request", "", {})],
        result=RemoteCallResult(is_error=False, output={"body": "가" * 100}),
    )
    adapter = WebAPIMCPAdapter(server_id="http", transport=backend)
    registry = MCPToolRegistry()
    registry.replace_server_tools("http", await adapter.discover_tools())
    gateway = MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        server_output_limits={adapter.server_id: 128},
    )

    result = await gateway.invoke(
        _invocation(tool_id=ToolId("web.http_request"), operation="http_request")
    )

    assert result.success is False
    assert result.output is None
    assert result.error is not None
    assert result.error.code == "tool_output_too_large"
    assert result.error.retryable is False
    assert result.metadata["output_omitted"] is True
    assert result.metadata["output_bytes"] > result.metadata["max_output_bytes"]


def test_gateway_rejects_output_limit_for_unknown_server() -> None:
    with pytest.raises(ValueError, match="unknown MCP servers"):
        MCPToolGateway(MCPToolRegistry(), {}, server_output_limits={"typo": 1024})


async def test_allowlist_hides_and_blocks_unsafe_remote_tools() -> None:
    backend = FakeTransport(tools=[
        RemoteTool("browser_snapshot", "", {}), RemoteTool("browser_run_code_unsafe", "", {}),
    ])
    filtered = AllowlistedMCPTransport(backend, ("browser_snapshot",))
    assert [tool.name for tool in await filtered.list_tools()] == ["browser_snapshot"]
    with pytest.raises(ValueError, match="not enabled"):
        await filtered.call_tool("browser_run_code_unsafe", {})
    assert backend.calls == []


async def test_allowlist_fails_on_upstream_tool_drift() -> None:
    filtered = AllowlistedMCPTransport(FakeTransport(), ("browser_snapshot",))
    with pytest.raises(ValueError, match="does not match"):
        await filtered.list_tools()


async def test_dry_run_does_not_invoke_transport() -> None:
    transport = FakeTransport(tools=[RemoteTool("http_request", "", {})])
    adapter = WebAPIMCPAdapter(server_id="http", transport=transport)
    registry = MCPToolRegistry()
    registry.replace_server_tools("http", await adapter.discover_tools())
    invocation = _invocation(tool_id=ToolId("web.http_request"), operation="http_request")
    invocation.execution_context.dry_run = True
    result = await MCPToolGateway(registry, {"http": adapter}).invoke(invocation)
    assert result.success and result.output == {"dry_run": True, "executed": False}
    assert transport.calls == []
