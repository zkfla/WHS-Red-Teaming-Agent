"""Offline checks for the operator-run browser smoke assertions."""

from __future__ import annotations

from typing import Any

import pytest

from examples.finance_browser_smoke import OPERATIONS, SmokeFailure, exercise
from redteam.capability.adapters.mcp.transport import (
    AllowlistedMCPTransport,
    RemoteCallResult,
    RemoteTool,
)
from redteam.capability.adapters.mcp.web_api import WebAPIMCPAdapter
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry


class BrowserTransport:
    def __init__(self, *, preserve_state: bool = True) -> None:
        self.calls: list[str] = []
        self.preserve_state = preserve_state

    async def list_tools(self) -> list[RemoteTool]:
        return browser_tools()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        self.calls.append(tool_name)
        if tool_name == "browser_snapshot":
            text = "Synthetic bank Test alias Preview alias"
            if self.preserve_state and "browser_click" in self.calls:
                text += " Synthetic Alice"
            return RemoteCallResult(is_error=False, output=text)
        return RemoteCallResult(is_error=False, output="ok")

    async def health_check(self) -> bool:
        return True


def browser_tools() -> list[RemoteTool]:
    schemas: dict[str, dict[str, Any]] = {
        "browser_navigate": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "browser_snapshot": {"type": "object"},
        "browser_fill_form": {
            "type": "object",
            "properties": {"fields": {"type": "array"}},
            "required": ["fields"],
        },
        "browser_click": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
        "browser_close": {"type": "object"},
        "browser_run_code_unsafe": {"type": "object", "properties": {"code": {"type": "string"}}},
    }
    return [RemoteTool(name, "", schema) for name, schema in schemas.items()]


async def make_gateway(transport: BrowserTransport) -> MCPToolGateway:
    allowed = AllowlistedMCPTransport(transport, tuple(OPERATIONS))
    adapter = WebAPIMCPAdapter(server_id="web-browser", transport=allowed)
    descriptors = await adapter.discover_tools()
    for descriptor in descriptors:
        descriptor.operations = list(OPERATIONS[descriptor.tool_name])
    registry = MCPToolRegistry()
    registry.replace_server_tools("web-browser", descriptors)
    return MCPToolGateway(
        registry,
        {"web-browser": adapter},
        {str(tool.tool_id): OPERATIONS[tool.tool_name] for tool in descriptors},
    )


async def test_browser_smoke_checks_session_and_does_not_call_denied_remote_tool() -> None:
    transport = BrowserTransport()
    await exercise(await make_gateway(transport), browser_tools(), "browser_run_code_unsafe")
    assert transport.calls == [
        "browser_navigate",
        "browser_snapshot",
        "browser_fill_form",
        "browser_click",
        "browser_snapshot",
    ]


async def test_browser_smoke_fails_when_second_snapshot_loses_state() -> None:
    transport = BrowserTransport(preserve_state=False)
    with pytest.raises(SmokeFailure, match="snapshot_after"):
        await exercise(await make_gateway(transport), browser_tools(), "browser_run_code_unsafe")
