"""Opt-in discovery acceptance for operator-installed external Web MCPs.

The repository never downloads or starts third-party desktop products.  An
operator opts in by exporting ``REDTEAM_EXTERNAL_MCP_SERVERS`` with a comma
separated list of configured server IDs (for example ``zap-recon,zap-web``).
The test then exercises the real transport, allowlist, Registry metadata and
health check.  It performs discovery only; no target request or scan starts.
"""

from __future__ import annotations

import os

import pytest

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.settings import MCPServerSettings


def _server_ids() -> list[str]:
    raw = os.environ.get("REDTEAM_EXTERNAL_MCP_SERVERS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("REDTEAM_EXTERNAL_MCP_SERVERS"),
    reason="set REDTEAM_EXTERNAL_MCP_SERVERS to run operator-installed MCP discovery",
)
async def test_installed_external_mcp_profiles_discover_only_allowlisted_tools() -> None:
    server_ids = _server_ids()
    assert server_ids
    assert len(server_ids) == len(set(server_ids))

    expected: dict[str, set[str]] = {}
    for server_id in server_ids:
        settings = MCPServerSettings.from_environment(server_id)
        assert settings.allowed_tools is not None
        assert settings.tool_operations is not None
        expected[server_id] = set(settings.allowed_tools)

    async with open_mcp_tool_gateway(server_ids) as gateway:
        descriptors = gateway.all()

    assert descriptors
    for server_id in server_ids:
        discovered = {
            str(descriptor.tool_id).split(".", 1)[1]
            for descriptor in descriptors
            if descriptor.server_id == server_id
        }
        assert discovered == expected[server_id]
        settings = MCPServerSettings.from_environment(server_id)
        assert settings.tool_operations is not None
        for descriptor in descriptors:
            if descriptor.server_id != server_id:
                continue
            assert descriptor.health_status.value == "healthy"
            assert descriptor.operations == list(
                settings.tool_operations[descriptor.tool_name]
            )
            assert descriptor.required_arguments == tuple(
                descriptor.input_schema.get("required", [])
            )
