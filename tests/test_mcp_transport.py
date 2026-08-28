import asyncio

import pytest

from whs_redteam.exceptions import ToolProtocolError, ToolTransportUnavailable
from whs_redteam.orchestration.mcp_transport import OfficialMCPTransport

MCPServer = pytest.importorskip("mcp.server").MCPServer


def test_official_transport_calls_in_memory_mcp_server() -> None:
    server = MCPServer("browser-test")

    @server.tool()
    def discover(target_url: str) -> dict[str, object]:
        return {"target_url": target_url, "pages": 2}

    transport = OfficialMCPTransport({"browser": server})
    result = asyncio.run(
        transport.call_tool(
            "browser", "discover", {"target_url": "https://example.test"}
        )
    )

    assert result == {"target_url": "https://example.test", "pages": 2}


def test_official_transport_rejects_unknown_server_without_connecting() -> None:
    transport = OfficialMCPTransport({"browser": object()})
    with pytest.raises(ToolTransportUnavailable, match="not configured"):
        asyncio.run(transport.call_tool("safe-http", "request", {}))


def test_official_transport_rejects_tool_outside_server_allowlist() -> None:
    server = MCPServer("browser-test")

    @server.tool()
    def discover() -> dict[str, int]:
        return {"pages": 1}

    transport = OfficialMCPTransport({"browser": server})
    with pytest.raises(ToolProtocolError, match="does not expose"):
        asyncio.run(transport.call_tool("browser", "arbitrary_shell", {}))


@pytest.mark.parametrize(
    "endpoint",
    (
        "file:///tmp/server",
        "https://user:pass@example.test/mcp",
        "https://example.test/mcp?token=secret",
        "https://example.test/mcp#fragment",
    ),
)
def test_official_transport_rejects_unsafe_endpoint_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        OfficialMCPTransport({"browser": endpoint})
