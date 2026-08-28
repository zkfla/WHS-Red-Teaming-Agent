"""Official MCP SDK v2 transport with structured-output-only handling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from whs_redteam.exceptions import ToolProtocolError, ToolTransportUnavailable


class OfficialMCPTransport:
    """Connect named servers to MCP SDK Client endpoints.

    Production endpoints are Streamable HTTP URLs. Tests may pass an in-memory
    MCPServer object, which the official SDK also supports.
    """

    def __init__(self, endpoints: Mapping[str, Any]) -> None:
        if not endpoints:
            raise ValueError("at least one MCP endpoint is required")
        self._endpoints = dict(endpoints)
        for server_name, endpoint in self._endpoints.items():
            if not server_name:
                raise ValueError("MCP server name must not be empty")
            if isinstance(endpoint, str):
                parsed = urlsplit(endpoint)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(
                        "MCP endpoint must be an absolute HTTP(S) URL without "
                        "credentials, query, or fragment"
                    )

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            endpoint = self._endpoints[server_name]
        except KeyError as exc:
            raise ToolTransportUnavailable(
                f"MCP endpoint is not configured: {server_name}"
            ) from exc
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover - exercised without the extra
            raise ToolTransportUnavailable(
                "MCP SDK is not installed; install the 'mcp' project extra"
            ) from exc

        try:
            result = None
            tool_missing = False
            async with Client(endpoint) as client:
                listed = await client.list_tools()
                available = {tool.name for tool in listed.tools}
                if tool_name not in available:
                    tool_missing = True
                else:
                    result = await client.call_tool(tool_name, dict(arguments))
        except Exception as exc:
            raise ToolTransportUnavailable(
                f"MCP call failed for server: {server_name}"
            ) from exc

        if tool_missing:
            raise ToolProtocolError(
                f"MCP server does not expose the bound tool: {tool_name}"
            )
        if result is None:
            raise ToolProtocolError("MCP server returned no tool result")
        if result.is_error:
            raise ToolProtocolError(f"MCP tool reported an error: {tool_name}")
        structured = result.structured_content
        if not isinstance(structured, Mapping):
            raise ToolProtocolError(
                "MCP tool must return structured object output"
            )
        return dict(structured)
