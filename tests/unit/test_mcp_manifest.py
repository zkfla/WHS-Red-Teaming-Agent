"""MCP discovery -> Registry manifest contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.manifest import validate_tool_manifest
from redteam.capability.settings import MCPConfigurationError
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId


def _descriptor(**updates: Any) -> ToolDescriptor:
    values: dict[str, Any] = {
        "tool_id": ToolId("web.http_request"),
        "server_id": "web-test",
        "tool_name": "http_request",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "path": {"type": "string"},
                "method": {"type": "string"},
            },
            "required": ["target_ref", "path"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"status": {"type": "integer"}},
            "required": ["status"],
        },
        "operations": ["http_request"],
        "target_types": ["endpoint", "service", "host"],
        "capability_tags": ["web", "api"],
        "risk_level": RiskLevel.MEDIUM,
        "health_status": ToolHealthStatus.HEALTHY,
    }
    values.update(updates)
    return ToolDescriptor(**values)


def test_manifest_preserves_agent_visible_metadata() -> None:
    descriptor = _descriptor()

    validate_tool_manifest(descriptor)

    assert descriptor.required_arguments == ("target_ref", "path")
    assert descriptor.optional_arguments == ("method",)
    assert descriptor.operations == ["http_request"]
    assert descriptor.target_types == ["endpoint", "service", "host"]
    assert descriptor.output_schema is not None
    assert descriptor.output_schema["required"] == ["status"]


@pytest.mark.parametrize(
    "updates",
    [
        {"input_schema": {"type": "object", "required": ["path"]}},
        {"operations": ["HTTP Request"]},
        {"target_types": ["unknown_target"]},
        {"output_schema": {"type": "not-a-json-schema-type"}},
        {"tool_id": ToolId("recon.http_request")},
        {"tool_id": ToolId("web.api.fetch")},
    ],
)
def test_manifest_rejects_metadata_or_schema_drift(updates: dict[str, Any]) -> None:
    with pytest.raises(MCPConfigurationError):
        validate_tool_manifest(_descriptor(**updates))


class _DiscoveredAdapter:
    def __init__(self, server_id: str, descriptor: ToolDescriptor) -> None:
        self.server_id = server_id
        self._descriptor = descriptor

    async def discover_tools(self) -> list[ToolDescriptor]:
        return [self._descriptor]

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        now = datetime.now(timezone.utc)
        return MCPCallResult(
            success=True,
            started_at=now,
            finished_at=now,
            output={"status": 200},
            error=None,
            metadata={},
        )

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY


async def test_bootstrap_validates_the_same_manifest_it_exposes_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_id = "web-manifest-test"
    prefix = "REDTEAM_MCP_WEB_MANIFEST_TEST"
    monkeypatch.setenv(f"{prefix}_KIND", "web_api")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", "unused-for-fake-transport")
    monkeypatch.setenv(
        f"{prefix}_TOOL_OPERATIONS_JSON",
        '{"http_request": ["http_request"]}',
    )
    descriptor = _descriptor(server_id=server_id)
    adapter = _DiscoveredAdapter(server_id, descriptor)
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_transport", lambda settings: object()
    )
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_adapter",
        lambda settings, transport, bundles=None: adapter,
    )

    gateway = await build_mcp_tool_gateway([server_id])
    registered = gateway.get(ToolId("web.http_request"))

    assert registered is not None
    assert registered.operations == ["http_request"]
    assert registered.required_arguments == ("target_ref", "path")
    assert registered.optional_arguments == ("method",)
    assert registered.target_types == ["endpoint", "service", "host"]
    assert registered.output_schema == descriptor.output_schema


async def test_bootstrap_fails_closed_on_discovery_manifest_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_id = "web-invalid-manifest-test"
    prefix = "REDTEAM_MCP_WEB_INVALID_MANIFEST_TEST"
    monkeypatch.setenv(f"{prefix}_KIND", "web_api")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", "unused-for-fake-transport")
    monkeypatch.setenv(
        f"{prefix}_TOOL_OPERATIONS_JSON",
        '{"http_request": ["http_request"]}',
    )
    descriptor = _descriptor(
        server_id=server_id,
        input_schema={"type": "object", "required": ["missing"]},
    )
    adapter = _DiscoveredAdapter(server_id, descriptor)
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_transport", lambda settings: object()
    )
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_adapter",
        lambda settings, transport, bundles=None: adapter,
    )

    with pytest.raises(MCPConfigurationError, match="absent from properties"):
        await build_mcp_tool_gateway([server_id])
