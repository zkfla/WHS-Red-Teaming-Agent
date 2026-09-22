"""Capability candidate view uses live discovery data without guessing fields."""

from __future__ import annotations

from redteam.capability.adapters.mcp.mobile import MobileMCPAdapter
from redteam.capability.adapters.mcp.transport import RemoteCallResult, RemoteTool
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId


def test_gateway_candidate_metadata_joins_registry_and_bindings() -> None:
    registry = MCPToolRegistry()
    descriptor = ToolDescriptor(
        tool_id=ToolId("analysis.decode_apk"),
        server_id="apktool",
        tool_name="decode_apk",
        input_schema={
            "type": "object",
            "properties": {"apk_path": {"type": "string"}, "force": {"type": "boolean"}},
            "required": ["apk_path"],
        },
        capability_tags=["analysis"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    registry.register(descriptor)
    gateway = MCPToolGateway(
        registry,
        adapters={},
        operation_bindings={"analysis.decode_apk": ("static_analysis",)},
        target_types={"analysis.decode_apk": "asset"},
        output_schemas={
            "analysis.decode_apk": {
                "type": "object", "properties": {"ok": {"type": "boolean"}}
            }
        },
    )
    candidates = gateway.list_candidate_metadata("analysis", "static_analysis")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.tool_id == ToolId("analysis.decode_apk")
    assert candidate.capability == "analysis"
    assert candidate.operation == "static_analysis"
    assert candidate.required_arguments == ("apk_path",)
    assert candidate.optional_arguments == ("force",)
    assert candidate.target_type == "asset"
    assert candidate.risk_level == RiskLevel.LOW
    assert candidate.output_schema is not None
    assert candidate.output_schema["properties"]["ok"]["type"] == "boolean"
    candidate.output_schema["properties"].clear()
    assert gateway.list_candidate_metadata("analysis", "static_analysis")[0].output_schema == {
        "type": "object", "properties": {"ok": {"type": "boolean"}}
    }


def test_gateway_candidate_metadata_preserves_unknowns() -> None:
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.unknown"),
            server_id="unknown-server",
            tool_name="unknown",
            capability_tags=["recon"],
        )
    )
    gateway = MCPToolGateway(
        registry,
        adapters={},
        operation_bindings={"recon.unknown": ("service_discovery",)},
    )
    candidate = gateway.list_candidate_metadata()[0]
    assert candidate.required_arguments is None
    assert candidate.optional_arguments is None
    assert candidate.target_type is None
    assert candidate.output_schema is None


def test_candidate_metadata_includes_every_registered_capability() -> None:
    registry = MCPToolRegistry()
    bindings: dict[str, tuple[str, ...]] = {}
    for capability, name, operation in (
        ("recon", "scan", "service_discovery"),
        ("web", "request", "http_request"),
        ("mobile", "launch", "app_launch"),
        ("analysis", "decode", "static_analysis"),
    ):
        tool_id = f"{capability}.{name}"
        registry.register(
            ToolDescriptor(
                tool_id=ToolId(tool_id),
                server_id=f"{capability}-server",
                tool_name=name,
                capability_tags=[capability],
                input_schema={"type": "object", "properties": {}},
            )
        )
        bindings[tool_id] = (operation,)
    gateway = MCPToolGateway(registry, adapters={}, operation_bindings=bindings)
    assert {(item.capability, item.operation) for item in gateway.list_candidate_metadata()} == {
        ("recon", "service_discovery"),
        ("web", "http_request"),
        ("mobile", "app_launch"),
        ("analysis", "static_analysis"),
    }


def test_multiple_candidates_for_one_operation_keep_individual_metadata() -> None:
    """``analysis.static_analysis`` with two apktool-shaped tools plus a
    local-fallback tool: each candidate must stay separately inspectable
    (own ``tool_id``/schema/risk), never collapsed or deduplicated just
    because they share a canonical operation."""

    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("analysis.decode_apk"),
            server_id="apktool",
            tool_name="decode_apk",
            input_schema={
                "type": "object",
                "properties": {"apk_path": {"type": "string"}},
                "required": ["apk_path"],
            },
            capability_tags=["analysis"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("analysis.get_manifest"),
            server_id="apktool",
            tool_name="get_manifest",
            input_schema={
                "type": "object",
                "properties": {"project_dir": {"type": "string"}},
                "required": ["project_dir"],
            },
            capability_tags=["analysis"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("analysis.apk_file_listing"),
            server_id="analysis-local",
            tool_name="apk_file_listing",
            input_schema={
                "type": "object",
                "properties": {"apk_ref": {"type": "string"}},
                "required": ["apk_ref"],
            },
            capability_tags=["analysis"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    gateway = MCPToolGateway(
        registry,
        adapters={},
        operation_bindings={
            "analysis.decode_apk": ("static_analysis",),
            "analysis.get_manifest": ("static_analysis",),
            "analysis.apk_file_listing": ("static_analysis",),
        },
        output_schemas={
            "analysis.apk_file_listing": {
                "type": "object", "properties": {"entries": {"type": "array"}}
            }
        },
        target_types={
            "analysis.decode_apk": "asset",
            "analysis.get_manifest": "asset",
            "analysis.apk_file_listing": "asset",
        },
    )

    candidates = gateway.list_candidate_metadata("analysis", "static_analysis")

    assert len(candidates) == 3
    by_tool_id = {str(c.tool_id): c for c in candidates}
    assert set(by_tool_id) == {
        "analysis.decode_apk",
        "analysis.get_manifest",
        "analysis.apk_file_listing",
    }
    assert by_tool_id["analysis.decode_apk"].required_arguments == ("apk_path",)
    assert by_tool_id["analysis.get_manifest"].required_arguments == ("project_dir",)
    assert by_tool_id["analysis.apk_file_listing"].required_arguments == ("apk_ref",)
    # Only the local-fallback tool has a known output schema -- the apktool
    # candidates correctly stay None (never guessed) alongside it.
    assert by_tool_id["analysis.decode_apk"].output_schema is None
    assert by_tool_id["analysis.get_manifest"].output_schema is None
    assert by_tool_id["analysis.apk_file_listing"].output_schema == {
        "type": "object", "properties": {"entries": {"type": "array"}}
    }


async def test_adapter_keeps_discovered_output_schema() -> None:
    class Transport:
        async def health_check(self) -> bool:
            return True

        async def list_tools(self) -> list[RemoteTool]:
            return [
                RemoteTool(
                    name="device_shell",
                    description="diagnostics",
                    input_schema={"type": "object", "properties": {}},
                    output_schema={"type": "object", "properties": {"output": {"type": "string"}}},
                )
            ]

        async def call_tool(self, tool_name: str, arguments: dict) -> RemoteCallResult:
            return RemoteCallResult(is_error=False, output={"output": "ok"})

    adapter = MobileMCPAdapter(server_id="test", transport=Transport())
    descriptor = (await adapter.discover_tools())[0]
    schema = adapter.output_schema_for_tool(descriptor.tool_id)
    assert schema is not None
    assert schema["properties"]["output"]["type"] == "string"
    schema["properties"].clear()
    assert adapter.output_schema_for_tool(descriptor.tool_id) == {
        "type": "object", "properties": {"output": {"type": "string"}}
    }
