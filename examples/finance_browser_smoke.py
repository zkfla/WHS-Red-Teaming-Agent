"""Operator-run Playwright MCP check inside the synthetic finance Compose network."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator

from redteam.capability.adapters.mcp.transport import RemoteTool, StreamableHTTPMCPTransport
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.settings import MCPServerSettings
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation, ToolResult
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId

SERVER_ID = "web-browser"
BANK_URL = "http://mock-bank:8080"
OPERATIONS = {
    "browser_navigate": ("http_request",),
    "browser_snapshot": ("response_analysis",),
    "browser_fill_form": ("auth_attempt",),
    "browser_click": ("auth_attempt",),
    "browser_close": (),
}
TIMEOUT_SECONDS = 45  # Same invocation bound as finance_mcp_smoke.py.


class SmokeFailure(RuntimeError):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")


def validate_discovery(
    remote_tools: list[RemoteTool], gateway: MCPToolGateway, settings: MCPServerSettings
) -> str:
    """Compare raw remote discovery with the operator-selected Registry snapshot."""
    if settings.allowed_tools is None or set(settings.allowed_tools) != set(OPERATIONS):
        raise SmokeFailure("discovery", "configured browser allowlist differs from smoke profile")
    if settings.tool_operations != OPERATIONS:
        raise SmokeFailure("discovery", "configured operation bindings differ from smoke profile")
    remote = {tool.name: tool for tool in remote_tools}
    if len(remote) != len(remote_tools) or not set(OPERATIONS) <= remote.keys():
        raise SmokeFailure("discovery", "remote browser tool names are missing or duplicated")
    registered = {str(tool.tool_id): tool for tool in gateway.all()}
    expected_ids = {f"web.{name}" for name in OPERATIONS}
    if set(registered) != expected_ids:
        raise SmokeFailure("registration", "Registry does not match selected browser tools")
    for name, operations in OPERATIONS.items():
        descriptor = registered[f"web.{name}"]
        if descriptor.tool_name != name or descriptor.server_id != SERVER_ID:
            raise SmokeFailure("registration", f"wrong descriptor for {name}")
        if tuple(descriptor.operations) != operations:
            raise SmokeFailure("registration", f"wrong operation binding for {name}")
        if descriptor.input_schema != remote[name].input_schema:
            raise SmokeFailure("registration", f"input schema differs for {name}")
        print(
            f"DISCOVERY {name}: "
            + json.dumps(remote[name].input_schema, ensure_ascii=False, sort_keys=True)
        )
    denied = "browser_run_code_unsafe"
    if denied not in remote or denied in settings.allowed_tools:
        raise SmokeFailure("discovery", "real unsafe browser tool unavailable for denial check")
    if gateway.get(ToolId(f"web.{denied}")) is not None:
        raise SmokeFailure("registration", "unsafe browser tool was registered")
    print(f"DISCOVERY denied remote tool: {denied}")
    return denied


def validate_arguments(remote: dict[str, RemoteTool], name: str, arguments: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(remote[name].input_schema)
        Draft202012Validator(remote[name].input_schema).validate(arguments)
    except Exception as exc:
        raise SmokeFailure("input", f"{name} arguments violate discovered schema: {exc}") from exc


async def exercise(
    gateway: MCPToolGateway, remote_tools: list[RemoteTool], denied_name: str
) -> None:
    remote = {tool.name: tool for tool in remote_tools}
    run_id = RunId("finance_browser_smoke")
    call_index = 0

    async def call(stage: str, name: str, operation: str, arguments: dict[str, Any]) -> ToolResult:
        nonlocal call_index
        if name != denied_name:
            validate_arguments(remote, name, arguments)
        call_index += 1
        invocation = ToolInvocation(
            invocation_id=InvocationId(f"browser_smoke_invocation_{call_index}"),
            action_id=ActionId(f"browser_smoke_action_{call_index}"),
            run_id=run_id,
            tool_id=ToolId(f"web.{name}"),
            operation=operation,
            arguments=arguments,
            timeout_seconds=TIMEOUT_SECONDS,
            execution_context=ExecutionContext(engagement_id=EngagementId("lab"), run_id=run_id),
            created_at=datetime.now(timezone.utc),
        )
        try:
            result = await gateway.invoke(invocation)
        except Exception as exc:
            raise SmokeFailure(stage, type(exc).__name__) from exc
        if (result.action_id, result.invocation_id, result.tool_id) != (
            invocation.action_id,
            invocation.invocation_id,
            invocation.tool_id,
        ):
            raise SmokeFailure(stage, "Action/Invocation/Tool ID changed")
        if name == denied_name:
            if result.success or result.error is None or result.error.code != "unknown_tool":
                raise SmokeFailure(stage, "discovered non-allowlisted tool was not rejected")
        elif not result.success:
            code = result.error.code if result.error else "missing_error"
            raise SmokeFailure(stage, f"Gateway call failed ({code})")
        print(f"PASS {stage}: action={result.action_id} invocation={result.invocation_id}")
        return result

    await call("navigate", "browser_navigate", "http_request", {"url": BANK_URL})
    before = await call("snapshot_before", "browser_snapshot", "response_analysis", {})
    snapshot = str(before.output)
    for text in ("Synthetic bank", "Test alias", "Preview alias"):
        if text not in snapshot:
            raise SmokeFailure("snapshot_before", f"missing visible text {text!r}")
    # The mock-bank markup supplies these selectors; discovery confirms target is accepted.
    await call(
        "fill_form",
        "browser_fill_form",
        "auth_attempt",
        {
            "fields": [
                {
                    "target": "#alias",
                    "name": "Test alias",
                    "type": "textbox",
                    "value": "Synthetic Alice",
                }
            ]
        },
    )
    await call("click", "browser_click", "auth_attempt", {"target": "#confirm"})
    after = await call("snapshot_after", "browser_snapshot", "response_analysis", {})
    if "Synthetic Alice" not in str(after.output):
        raise SmokeFailure(
            "snapshot_after", "alias preview is absent; browser state was not retained"
        )
    await call("allowlist", denied_name, "runtime_instrumentation", {})
    print("PASS: browser discovery, Registry, UI session continuity, IDs, allowlist")


async def main() -> None:
    settings = MCPServerSettings.from_environment(SERVER_ID)
    if settings.endpoint is None:
        raise SmokeFailure("discovery", "browser endpoint is not configured")
    # Raw discovery is read-only; the Gateway session below performs every UI call.
    transport = StreamableHTTPMCPTransport(settings.endpoint, headers=settings.request_headers())
    try:
        async with transport.session():
            remote_tools = await transport.list_tools()
    except Exception as exc:
        raise SmokeFailure("discovery", type(exc).__name__) from exc
    print("REMOTE TOOLS: " + ", ".join(tool.name for tool in remote_tools))
    try:
        async with open_mcp_tool_gateway([SERVER_ID]) as gateway:
            denied_name = validate_discovery(remote_tools, gateway, settings)
            await exercise(gateway, remote_tools, denied_name)
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure("registration", type(exc).__name__) from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SmokeFailure as exc:
        raise SystemExit(f"FAIL {exc}") from exc
