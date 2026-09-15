"""MCP SDK isolation and transport-neutral wire results.

Only this module imports the optional official MCP Python SDK. Adapters
consume the small ``MCPTransport`` Protocol, which keeps them deterministic
and testable without a live server or external network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class RemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RemoteCallResult:
    is_error: bool
    output: dict[str, Any] | str | None


class MCPTransport(Protocol):
    async def list_tools(self) -> list[RemoteTool]: ...

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> RemoteCallResult: ...

    async def health_check(self) -> bool: ...


class StreamableHTTPMCPTransport:
    """Official MCP Python SDK v2 client over Streamable HTTP."""

    def __init__(self, endpoint: str) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MCP endpoint must be an absolute http(s) URL")
        self._endpoint = endpoint

    async def list_tools(self) -> list[RemoteTool]:
        Client = _load_mcp_client()
        discovered: list[RemoteTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        async with Client(self._endpoint) as client:
            while True:
                listing = await client.list_tools(cursor=cursor)
                discovered.extend(
                    RemoteTool(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=dict(tool.input_schema),
                    )
                    for tool in listing.tools
                )
                next_cursor = listing.next_cursor
                if next_cursor is None:
                    break
                if next_cursor in seen_cursors:
                    raise RuntimeError("MCP server returned a repeated tools cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor

        return discovered

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> RemoteCallResult:
        Client = _load_mcp_client()
        async with Client(self._endpoint) as client:
            result = await client.call_tool(tool_name, arguments=arguments)

        structured = result.structured_content
        if structured is not None:
            output: dict[str, Any] | str | None = dict(structured)
        elif len(result.content) == 1 and isinstance(
            getattr(result.content[0], "text", None), str
        ):
            output = result.content[0].text
        elif result.content:
            output = {
                "content": [
                    block.model_dump(mode="json", by_alias=True) for block in result.content
                ]
            }
        else:
            output = None

        return RemoteCallResult(is_error=bool(result.is_error), output=output)

    async def health_check(self) -> bool:
        try:
            Client = _load_mcp_client()
            async with Client(self._endpoint):
                return True
        except Exception:
            return False


def _load_mcp_client() -> type[Any]:
    try:
        from mcp import Client
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            'MCP support is not installed; run pip install -e ".[capability]"'
        ) from exc
    return Client
