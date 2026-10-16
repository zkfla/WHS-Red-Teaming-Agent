"""Proxy replay capacity through the configured MCP gateway, with local fixtures only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer

from redteam.capability import bootstrap
from redteam.capability.adapters.mcp.transport import (
    RemoteCallResult,
    RemoteTool,
    remote_tool_metadata,
)
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.servers import proxy
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse
from redteam.capability.settings import MCPServerSettings
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from redteam.core.tool_binding import SELECTED_TOOL


def policy() -> ServerPolicy:
    paths = [f"/item-{index}" for index in range(7)]
    return ServerPolicy.model_validate({
        "targets": {
            "bank": {
                "origin": "https://bank.test",
                "allowed_paths": paths,
                "replayable_paths": paths,
            },
        },
        "allowed_networks": ["192.0.2.0/24"],
        "max_concurrency": 7,
        "proxy_replay_enabled": True,
    })


def capture() -> bytes:
    return json.dumps({"log": {"entries": [
        {
            "request": {"method": "GET", "url": f"https://bank.test/item-{index}"},
            "response": {"status": 200, "headers": [], "content": {"text": "fixture"}},
        }
        for index in range(7)
    ]}}).encode()


class InProcessTransport:
    """Expose the real MCP server object through the generic adapter contract."""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self._client: Client | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        async with Client(self.server) as client:
            self._client = client
            try:
                yield
            finally:
                self._client = None

    async def list_tools(self) -> list[RemoteTool]:
        if self._client is None:
            async with Client(self.server) as client:
                listing = await client.list_tools()
        else:
            listing = await self._client.list_tools()
        return [
            RemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
                output_schema=dict(tool.output_schema) if tool.output_schema else None,
                metadata=remote_tool_metadata(tool),
            )
            for tool in listing.tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        if self._client is None:
            async with Client(self.server) as client:
                result = await client.call_tool(tool_name, arguments)
        else:
            result = await self._client.call_tool(tool_name, arguments)
        output = dict(result.structured_content) if result.structured_content is not None else None
        return RemoteCallResult(is_error=bool(result.is_error), output=output)

    async def health_check(self) -> bool:
        return True


def invocation(index: int, flow_ref: str) -> ToolInvocation:
    run_id = RunId("run_proxy_capacity")
    return ToolInvocation(
        invocation_id=InvocationId(f"inv_proxy_{index}"),
        action_id=ActionId(f"action_proxy_{index}"),
        run_id=run_id,
        tool_id=ToolId("web.web-proxy.proxy_replay_flow"),
        binding_mode=SELECTED_TOOL,
        arguments={"flow_ref": flow_ref},
        timeout_seconds=10,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_proxy_capacity"), run_id=run_id,
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_gateway_and_proxy_allow_six_independent_paths_and_cap_at_six(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = policy()
    capture_path = tmp_path / "capture.har"
    capture_path.write_bytes(capture())
    backend = proxy.HarCaptureBackend.load(str(capture_path), configured)
    flow_refs = [flow.flow_ref for flow in backend.list_flows()]
    assert len(flow_refs) == 7

    condition = asyncio.Condition()
    release = asyncio.Event()
    active = peak = started = 0
    started_paths: set[str] = set()
    same_path_calls = 0
    same_path_mode = False
    same_path_first = asyncio.Event()
    same_path_second = asyncio.Event()
    release_same_path = asyncio.Event()

    async def fake_request(
        _policy: ServerPolicy,
        _target_ref: str,
        path: str,
        **kwargs: Any,
    ) -> CapturedResponse:
        nonlocal active, peak, started, same_path_calls
        async with condition:
            active += 1
            started += 1
            started_paths.add(path)
            peak = max(peak, active)
            condition.notify_all()
        if path == "/item-0" and same_path_mode:
            same_path_calls += 1
            if same_path_calls == 1:
                same_path_first.set()
                await release_same_path.wait()
            else:
                same_path_second.set()
        else:
            await release.wait()
        async with condition:
            active -= 1
            condition.notify_all()
        return CapturedResponse(200, {}, b"synthetic")

    monkeypatch.setattr(proxy, "request", fake_request)
    server = proxy.create_server(configured, str(capture_path))
    transport = InProcessTransport(server)
    env = {
        "REDTEAM_MCP_WEB_PROXY_KIND": "web_api",
        "REDTEAM_MCP_WEB_PROXY_TRANSPORT": "stdio",
        "REDTEAM_MCP_WEB_PROXY_COMMAND": "unused-fixture-command",
        "REDTEAM_MCP_WEB_PROXY_MAX_CONCURRENCY": "6",
    }
    monkeypatch.setattr(bootstrap, "_create_transport", lambda _settings: transport)
    assert MCPServerSettings.from_environment("web-proxy", env=env).max_concurrency == 6

    async with open_mcp_tool_gateway(
        ["web-proxy"], env=env, health_refresh_enabled=False,
    ) as gateway:
        calls = [
            asyncio.create_task(gateway.invoke(invocation(index, flow_ref)))
            for index, flow_ref in enumerate(flow_refs)
        ]
        reached_six = True
        observed_started = observed_peak = 0
        observed_paths: set[str] = set()
        try:
            async with condition:
                try:
                    await asyncio.wait_for(condition.wait_for(lambda: started >= 6), 3)
                except asyncio.TimeoutError:
                    reached_six = False
                observed_started = started
                observed_peak = peak
                observed_paths = set(started_paths)
        finally:
            release.set()
        results = await asyncio.gather(*calls)

        same_path_mode = True
        same_first = asyncio.create_task(gateway.invoke(invocation(8, flow_refs[0])))
        await same_path_first.wait()
        same_second = asyncio.create_task(gateway.invoke(invocation(9, flow_refs[0])))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(same_path_second.wait(), 0.05)
        finally:
            release_same_path.set()
        same_results = await asyncio.gather(same_first, same_second)

    assert reached_six
    assert observed_started == 6
    assert observed_peak == 6
    assert observed_paths == {f"/item-{index}" for index in range(6)}
    assert started == 9
    assert peak == 6
    assert all(result.success for result in [*results, *same_results])
