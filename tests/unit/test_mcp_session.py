"""Scoped connections survive child calls and close without background leaks."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult

from redteam.capability import bootstrap
from redteam.capability.adapters.mcp.transport import StreamableHTTPMCPTransport
from redteam.core.contracts.tool import PROVIDER_RESULT_EXECUTION_UNCERTAIN


def fake_connection(events: list[str]) -> Any:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        events.append("open")
        count = 0

        async def call_tool(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            count += 1
            return SimpleNamespace(structured_content={"count": count}, is_error=False)

        async def list_tools(**kwargs: Any) -> Any:
            return SimpleNamespace(tools=[SimpleNamespace(
                name="ping", description="ping", input_schema={"type": "object"},
            )], next_cursor=None)

        try:
            yield SimpleNamespace(call_tool=call_tool, list_tools=list_tools)
        finally:
            events.append("close")

    return connect


async def test_session_reuses_connection_and_rejects_calls_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transport = StreamableHTTPMCPTransport("http://mcp.test/mcp")
    monkeypatch.setattr(transport, "_connect_client", fake_connection(events))
    async with transport.session():
        await transport.list_tools()
        for number in (1, 2):
            result = await asyncio.wait_for(transport.call_tool("ping", {}), timeout=1)
            assert result.output == {"count": number}
        with pytest.raises(RuntimeError, match="already open"):
            async with transport.session():
                pytest.fail("nested session allowed")
        assert events == ["open"]
    assert events == ["open", "close"]
    with pytest.raises(RuntimeError, match="scope has ended"):
        await transport.call_tool("ping", {})


async def test_session_closes_on_owner_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    transport = StreamableHTTPMCPTransport("http://mcp.test/mcp")
    monkeypatch.setattr(transport, "_connect_client", fake_connection(events))
    ready = asyncio.Event()

    async def owner() -> None:
        async with transport.session():
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["open", "close"]


async def test_transport_preserves_result_meta_separately_from_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_result = CallToolResult(
        content=[],
        is_error=True,
        structured_content={"diagnostic": {"execution_uncertain": False}},
        meta={PROVIDER_RESULT_EXECUTION_UNCERTAIN: True},
    )

    async def call_tool(*args: Any, **kwargs: Any) -> CallToolResult:
        return raw_result

    @asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        yield SimpleNamespace(call_tool=call_tool)

    transport = StreamableHTTPMCPTransport("http://mcp.test/mcp")
    monkeypatch.setattr(transport, "_connect_client", connect)
    async with transport.session():
        result = await transport.call_tool("probe", {})

    assert result.output == {"diagnostic": {"execution_uncertain": False}}
    assert result.metadata == {PROVIDER_RESULT_EXECUTION_UNCERTAIN: True}


async def test_partial_bootstrap_failure_closes_prior_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transport = StreamableHTTPMCPTransport("http://mcp.test/mcp")
    monkeypatch.setattr(transport, "_connect_client", fake_connection(events))
    monkeypatch.setattr(bootstrap, "_create_transport", lambda settings: transport)
    monkeypatch.setenv("REDTEAM_MCP_GOOD_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_GOOD_ENDPOINT", "https://mcp.test/mcp")
    monkeypatch.setenv("REDTEAM_MCP_BAD_KIND", "invalid-kind")
    with pytest.raises(ValueError):
        async with bootstrap.open_mcp_tool_gateway(["good", "bad"]):
            pytest.fail("invalid second configuration was accepted")
    assert events == ["open", "close"]
