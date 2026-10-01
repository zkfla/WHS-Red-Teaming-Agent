"""Tool health refresh is a live, bounded liveness signal.

Covers the capability-owned refresh path added on top of discovery:

  - a discovery-time ``UNHEALTHY`` snapshot is re-probed and can recover
    before an already-approved invocation is executed;
  - a refresh that reports ``UNHEALTHY`` blocks execution without ever
    reaching the adapter;
  - every probe is time-bounded, and a hung/failing probe degrades to the
    last known status instead of failing the invocation (health is *not*
    authorization — the Control Plane decision already happened);
  - the planner can refresh on demand before reading candidates;
  - the refresh interval can throttle repeated probes;
  - the Registry/Catalog expose the refreshed state read-only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import (
    ExecutionContext,
    ToolDescriptor,
    ToolInvocation,
)
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId

SERVER = "recon-server"
OTHER_SERVER = "web-server"
TOOL_ID = ToolId("recon.probe")
OPERATION = "service_discovery"


def _descriptor(
    tool_id: ToolId = TOOL_ID,
    server_id: str = SERVER,
    *,
    health: ToolHealthStatus = ToolHealthStatus.UNKNOWN,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        server_id=server_id,
        tool_name=str(tool_id).rpartition(".")[2],
        operations=[OPERATION] if str(tool_id).startswith("recon") else ["http_request"],
        capability_tags=["recon"] if str(tool_id).startswith("recon") else ["web"],
        health_status=health,
    )


def _invocation() -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId("inv_health"),
        action_id=ActionId("action_health"),
        run_id=RunId("run_health"),
        tool_id=TOOL_ID,
        operation=OPERATION,
        arguments={},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_health"), run_id=RunId("run_health")
        ),
        created_at=datetime.now(timezone.utc),
    )


class _FakeAdapter:
    """Deterministic adapter whose health can flip independently of discovery."""

    def __init__(
        self,
        server_id: str = SERVER,
        *,
        health: ToolHealthStatus = ToolHealthStatus.HEALTHY,
        hang: bool = False,
        raises: bool = False,
    ) -> None:
        self.server_id = server_id
        self.health = health
        self.hang = hang
        self.raises = raises
        self.health_calls = 0
        self.invocations = 0

    async def discover_tools(self) -> list[ToolDescriptor]:
        return [ToolDescriptor(tool_id=TOOL_ID, server_id=SERVER, tool_name="probe")]

    async def invoke(self, tool: ToolDescriptor, arguments: dict) -> MCPCallResult:
        self.invocations += 1
        now = datetime.now(timezone.utc)
        return MCPCallResult(success=True, output={"ok": True}, started_at=now, finished_at=now)

    async def health_check(self) -> ToolHealthStatus:
        self.health_calls += 1
        if self.raises:
            raise RuntimeError("probe exploded")
        if self.hang:
            await asyncio.sleep(10)
        return self.health


def _gateway(
    adapter: _FakeAdapter,
    *,
    registered_health: ToolHealthStatus = ToolHealthStatus.UNKNOWN,
    enabled: bool = True,
    interval: float = 0.0,
    timeout: float = 5.0,
) -> MCPToolGateway:
    registry = MCPToolRegistry()
    registry.register(
        _descriptor(health=registered_health).model_copy(update={"server_id": adapter.server_id})
    )
    return MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        {str(TOOL_ID): (OPERATION,)},
        health_refresh_enabled=enabled,
        health_refresh_interval_seconds=interval,
        health_refresh_timeout_seconds=timeout,
    )


# --- refresh before invocation -----------------------------------------


async def test_invocation_refreshes_discovery_snapshot_and_recovers() -> None:
    adapter = _FakeAdapter(health=ToolHealthStatus.HEALTHY)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.UNHEALTHY)

    result = await gateway.invoke(_invocation())

    assert result.success is True
    assert adapter.invocations == 1
    assert adapter.health_calls == 1
    assert gateway.get(TOOL_ID).health_status is ToolHealthStatus.HEALTHY


async def test_invocation_blocks_when_refresh_reports_unhealthy() -> None:
    adapter = _FakeAdapter(health=ToolHealthStatus.UNHEALTHY)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.HEALTHY)

    result = await gateway.invoke(_invocation())

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_unhealthy"
    assert adapter.invocations == 0
    assert adapter.health_calls == 1
    assert gateway.get(TOOL_ID).health_status is ToolHealthStatus.UNHEALTHY


async def test_refresh_is_time_bounded_and_degrades_to_last_known_status() -> None:
    adapter = _FakeAdapter(health=ToolHealthStatus.HEALTHY, hang=True)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.HEALTHY, timeout=0.01)

    result = await asyncio.wait_for(gateway.invoke(_invocation()), timeout=1.0)

    # The hung probe neither stalls nor denies the approved call.
    assert result.success is True
    assert adapter.invocations == 1
    assert gateway.get(TOOL_ID).health_status is ToolHealthStatus.HEALTHY
    assert gateway.last_health_refresh(SERVER) is not None


async def test_probe_failure_is_not_an_authorization_or_execution_failure() -> None:
    adapter = _FakeAdapter(raises=True)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.HEALTHY)

    result = await gateway.invoke(_invocation())

    assert result.success is True
    assert adapter.invocations == 1
    assert gateway.get(TOOL_ID).health_status is ToolHealthStatus.HEALTHY


async def test_refresh_disabled_uses_the_discovery_snapshot() -> None:
    adapter = _FakeAdapter(health=ToolHealthStatus.UNHEALTHY)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.HEALTHY, enabled=False)

    result = await gateway.invoke(_invocation())

    assert result.success is True
    assert adapter.health_calls == 0


# --- planner refresh path ----------------------------------------------


async def test_refresh_health_updates_planner_candidates() -> None:
    adapter = _FakeAdapter(health=ToolHealthStatus.UNHEALTHY)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.HEALTHY)

    assert [tool.tool_id for tool in gateway.list_tools_for_action("recon", OPERATION)] == [
        TOOL_ID
    ]

    statuses = await gateway.refresh_health(force=True)

    assert statuses == {SERVER: ToolHealthStatus.UNHEALTHY}
    assert gateway.list_tools_for_action("recon", OPERATION) == []
    assert [
        tool.tool_id
        for tool in gateway.list_tools_for_action("recon", OPERATION, include_unhealthy=True)
    ] == [TOOL_ID]
    assert gateway.health_snapshot() == {SERVER: ToolHealthStatus.UNHEALTHY}


async def test_refresh_health_rejects_unknown_server() -> None:
    gateway = _gateway(_FakeAdapter())

    with pytest.raises(ValueError, match="unknown MCP servers"):
        await gateway.refresh_health(["typo"])


async def test_refresh_interval_throttles_repeated_probes() -> None:
    adapter = _FakeAdapter(health=ToolHealthStatus.HEALTHY)
    gateway = _gateway(adapter, registered_health=ToolHealthStatus.HEALTHY, interval=3600.0)

    assert (await gateway.invoke(_invocation())).success is True
    assert (await gateway.invoke(_invocation())).success is True
    assert adapter.health_calls == 0  # discovery already counts as the first probe

    await gateway.refresh_health(force=True)

    assert adapter.health_calls == 1


# --- bounds / public read surface --------------------------------------


def test_gateway_rejects_invalid_health_refresh_bounds() -> None:
    registry = MCPToolRegistry()
    with pytest.raises(ValueError, match="interval"):
        MCPToolGateway(registry, {}, health_refresh_interval_seconds=-1.0)
    with pytest.raises(ValueError, match="timeout"):
        MCPToolGateway(registry, {}, health_refresh_timeout_seconds=0.0)


def test_registry_update_server_health_reports_change_and_reads_back() -> None:
    registry = MCPToolRegistry()
    registry.register(_descriptor(ToolId("recon.a"), SERVER, health=ToolHealthStatus.HEALTHY))
    registry.register(_descriptor(ToolId("recon.b"), SERVER, health=ToolHealthStatus.HEALTHY))
    registry.register(_descriptor(ToolId("web.c"), OTHER_SERVER, health=ToolHealthStatus.HEALTHY))

    assert (
        registry.update_server_health(SERVER, ToolHealthStatus.UNHEALTHY) is True
    )
    assert registry.update_server_health(SERVER, ToolHealthStatus.UNHEALTHY) is False
    assert registry.server_health(SERVER) is ToolHealthStatus.UNHEALTHY
    assert registry.server_health(OTHER_SERVER) is ToolHealthStatus.HEALTHY
    assert registry.server_health("missing") is None
    assert registry.update_server_health("missing", ToolHealthStatus.UNHEALTHY) is False


def test_catalog_health_snapshot_groups_by_server() -> None:
    registry = MCPToolRegistry()
    registry.register(_descriptor(ToolId("recon.a"), SERVER, health=ToolHealthStatus.HEALTHY))
    registry.register(_descriptor(ToolId("web.c"), OTHER_SERVER, health=ToolHealthStatus.UNKNOWN))

    catalog = MCPToolCatalog(
        registry,
        {"recon.a": (OPERATION,), "web.c": ("http_request",)},
    )

    assert catalog.health_snapshot() == {
        SERVER: ToolHealthStatus.HEALTHY,
        OTHER_SERVER: ToolHealthStatus.UNKNOWN,
    }
    # Snapshot is read-only: mutating it must not touch the Registry.
    catalog.health_snapshot()[SERVER] = ToolHealthStatus.UNHEALTHY
    assert registry.server_health(SERVER) is ToolHealthStatus.HEALTHY


async def test_bootstrap_threads_health_refresh_settings() -> None:
    gateway = await build_mcp_tool_gateway(
        [],
        health_refresh_interval_seconds=12.5,
        health_refresh_timeout_seconds=0.5,
    )

    assert gateway.health_snapshot() == {"example-mcp": ToolHealthStatus.HEALTHY}
    assert gateway.last_health_refresh("example-mcp") is not None
