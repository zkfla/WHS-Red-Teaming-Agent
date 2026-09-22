"""MCP SDK isolation and transport-neutral wire results.

Only this module imports the optional official MCP Python SDK. Adapters
consume the small ``MCPTransport`` Protocol, which keeps them deterministic
and testable without a live server or external network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
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

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult: ...

    async def health_check(self) -> bool: ...


class AllowlistedMCPTransport:
    """Only expose and invoke the operator-selected remote tool names."""

    def __init__(self, transport: MCPTransport, allowed_tools: tuple[str, ...]) -> None:
        self._transport = transport
        self._allowed = frozenset(allowed_tools)

    async def list_tools(self) -> list[RemoteTool]:
        tools = await self._transport.list_tools()
        if self._allowed - {tool.name for tool in tools}:
            raise ValueError("configured tool allowlist does not match the server catalog")
        return [tool for tool in tools if tool.name in self._allowed]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        if tool_name not in self._allowed:
            raise ValueError("tool is not enabled by the operator")
        return await self._transport.call_tool(tool_name, arguments)

    async def health_check(self) -> bool:
        return await self._transport.health_check()


class _SDKMCPTransport:
    """Shared MCP SDK result mapping for concrete connection transports."""

    _session_client: Any = None
    _session_ended = False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        """Keep one client alive within its owning task's explicit lifecycle.

        Enter/exit in the same task (SDK task groups require this). Child tasks
        may call tools, but must finish before the session exits. No reconnect
        or automatic replay is performed after a failure.
        """
        if self._session_client is not None:
            raise RuntimeError("MCP session is already open")
        async with self._connect_client() as client:
            self._session_ended = False
            self._session_client = client
            try:
                yield
            finally:
                self._session_client = None
                self._session_ended = True

    @asynccontextmanager
    async def _open_client(self) -> AsyncIterator[Any]:
        if self._session_client is not None:
            yield self._session_client
        elif self._session_ended:
            raise RuntimeError("MCP session scope has ended")
        else:
            async with self._connect_client() as client:
                yield client

    async def list_tools(self) -> list[RemoteTool]:
        discovered: list[RemoteTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        async with self._open_client() as client:
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

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        async with self._open_client() as client:
            result = await client.call_tool(tool_name, arguments=arguments)

        structured = result.structured_content
        if structured is not None:
            output: dict[str, Any] | str | None = dict(structured)
        elif len(result.content) == 1 and isinstance(getattr(result.content[0], "text", None), str):
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
            async with self._open_client():
                return True
        except Exception:
            return False

    def _connect_client(self) -> Any:
        raise NotImplementedError


class StreamableHTTPMCPTransport(_SDKMCPTransport):
    """Official MCP Python SDK v2 client over Streamable HTTP."""

    def __init__(self, endpoint: str, *, headers: Mapping[str, str] | None = None) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MCP endpoint must be an absolute http(s) URL")
        self._endpoint = endpoint
        self._headers = dict(headers or {})

    @asynccontextmanager
    async def _connect_client(self) -> AsyncIterator[Any]:
        Client = _load_mcp_client()
        if not self._headers:
            async with Client(self._endpoint) as client:
                yield client
            return

        try:
            from httpx2 import AsyncClient
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:  # pragma: no cover - optional install guard
            raise RuntimeError('MCP support is not installed; run pip install -e "."') from exc

        async with AsyncClient(headers=self._headers) as http_client:
            transport = streamable_http_client(self._endpoint, http_client=http_client)
            async with Client(transport) as client:
                yield client


class StdioMCPTransport(_SDKMCPTransport):
    """Official MCP Python SDK v2 client for a local argv-based MCP process.

    No shell is involved. The child receives only the explicit environment
    mapping plus the SDK's minimal safe environment.
    """

    def __init__(
        self,
        command: str,
        *,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command.strip() or "\x00" in command or "\n" in command:
            raise ValueError("MCP stdio command must be a non-empty executable name")
        if any("\x00" in argument for argument in args):
            raise ValueError("MCP stdio arguments must not contain NUL bytes")
        self._command = command
        self._args = tuple(args)
        self._env = dict(env or {})

    @asynccontextmanager
    async def _connect_client(self) -> AsyncIterator[Any]:
        Client = _load_mcp_client()
        try:
            from mcp import StdioServerParameters
        except ImportError as exc:  # pragma: no cover - optional install guard
            raise RuntimeError('MCP support is not installed; run pip install -e "."') from exc

        parameters = StdioServerParameters(
            command=self._command,
            args=list(self._args),
            env=self._env or None,
        )
        async with Client(parameters) as client:
            yield client


def _load_mcp_client() -> type[Any]:
    try:
        from mcp import Client
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError('MCP support is not installed; run pip install -e "."') from exc
    return Client
