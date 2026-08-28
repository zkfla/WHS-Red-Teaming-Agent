import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.exceptions import (
    ProbeRegistrationError,
    ToolProtocolError,
    ToolTransportUnavailable,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    Capability,
    RiskLevel,
    ToolBackend,
    ToolCall,
)
from whs_redteam.orchestration.adapters import (
    MCPProbeAdapter,
    MCPToolBinding,
    MCPTransport,
    ProbeContext,
)


class RecordingTransport(MCPTransport):
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append((server_name, tool_name, arguments))
        return self.response


def tool_call(capability: Capability = Capability.DISCOVERY) -> ToolCall:
    return ToolCall(
        action_id="action-1",
        queue_id="queue-1",
        run_id="run-1",
        engagement_name="adapter-test",
        capability=capability,
        target_url="https://example.test/catalog",
        method="get",
        risk_level=RiskLevel.PASSIVE,
        reason="bounded test",
    )


def context(tmp_path: Path) -> tuple[InventoryStore, ProbeContext]:
    store = InventoryStore(tmp_path / "inventory.db")
    return store, ProbeContext(
        "run-1", store, AuditLogger(tmp_path / "events.jsonl")
    )


def discovery_adapter(transport: MCPTransport | None, **kwargs) -> MCPProbeAdapter:
    return MCPProbeAdapter(
        (
            MCPToolBinding(
                capability=Capability.DISCOVERY,
                server_name="browser",
                tool_name="discover",
            ),
        ),
        transport,
        **kwargs,
    )


def test_mcp_adapter_calls_only_the_capability_allowlisted_tool(tmp_path: Path) -> None:
    transport = RecordingTransport({"pages": 3})
    adapter = discovery_adapter(transport)
    store, probe_context = context(tmp_path)
    try:
        result = asyncio.run(adapter.execute(tool_call(), probe_context))
    finally:
        store.close()

    assert result.backend is ToolBackend.MCP
    assert result.tool_name == "browser.discover"
    assert result.output == {"pages": 3}
    server, tool, arguments = transport.calls[0]
    assert (server, tool) == ("browser", "discover")
    assert arguments["request"]["capability"] == "discovery"
    assert arguments["request"]["method"] == "GET"
    assert "reason" not in arguments["request"]


def test_mcp_adapter_rejects_unbound_capability_before_transport(tmp_path: Path) -> None:
    transport = RecordingTransport({"unexpected": True})
    adapter = discovery_adapter(transport)
    store, probe_context = context(tmp_path)
    try:
        with pytest.raises(ProbeRegistrationError, match="no MCP tool"):
            asyncio.run(
                adapter.execute(tool_call(Capability.SQLI_SAFE), probe_context)
            )
    finally:
        store.close()
    assert transport.calls == []


def test_mcp_adapter_without_transport_fails_closed(tmp_path: Path) -> None:
    adapter = discovery_adapter(None)
    store, probe_context = context(tmp_path)
    try:
        with pytest.raises(ToolTransportUnavailable, match="not configured"):
            asyncio.run(adapter.execute(tool_call(), probe_context))
    finally:
        store.close()


def test_mcp_adapter_rejects_cross_run_call(tmp_path: Path) -> None:
    transport = RecordingTransport({"pages": 1})
    adapter = discovery_adapter(transport)
    store, probe_context = context(tmp_path)
    try:
        bad_context = ProbeContext(
            "different-run", probe_context.inventory, probe_context.audit_logger
        )
        with pytest.raises(ToolProtocolError, match="run_id"):
            asyncio.run(adapter.execute(tool_call(), bad_context))
    finally:
        store.close()
    assert transport.calls == []


def test_mcp_adapter_caps_response_size(tmp_path: Path) -> None:
    adapter = discovery_adapter(
        RecordingTransport({"body": "x" * 100}), max_response_bytes=32
    )
    store, probe_context = context(tmp_path)
    try:
        with pytest.raises(ToolProtocolError, match="size limit"):
            asyncio.run(adapter.execute(tool_call(), probe_context))
    finally:
        store.close()


def test_mcp_adapter_rejects_non_json_response_value(tmp_path: Path) -> None:
    adapter = discovery_adapter(RecordingTransport({"value": object()}))
    store, probe_context = context(tmp_path)
    try:
        with pytest.raises(ToolProtocolError, match="not JSON serializable"):
            asyncio.run(adapter.execute(tool_call(), probe_context))
    finally:
        store.close()


def test_mcp_adapter_rejects_duplicate_capability_binding() -> None:
    bindings = (
        MCPToolBinding(Capability.DISCOVERY, "browser-a", "discover"),
        MCPToolBinding(Capability.DISCOVERY, "browser-b", "discover"),
    )
    with pytest.raises(ProbeRegistrationError, match="already bound"):
        MCPProbeAdapter(bindings, RecordingTransport({}))


def test_sensitive_mcp_binding_requires_server_side_signed_receipt() -> None:
    adapter = MCPProbeAdapter(
        (MCPToolBinding(Capability.IDOR_SAFE, "http", "compare_object"),),
        RecordingTransport({}),
    )
    with pytest.raises(ToolProtocolError, match="server-side receipt"):
        adapter.resolve(Capability.IDOR_SAFE)
