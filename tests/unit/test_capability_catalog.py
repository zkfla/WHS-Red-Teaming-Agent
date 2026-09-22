"""Canonical action to MCP ToolId candidate resolution tests."""

from __future__ import annotations

import pytest

from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId


def _tool(
    tool_id: str,
    tool_name: str,
    *,
    health: ToolHealthStatus = ToolHealthStatus.HEALTHY,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(tool_id),
        server_id=f"server-{tool_name}",
        tool_name=tool_name,
        health_status=health,
    )


def _registry() -> MCPToolRegistry:
    registry = MCPToolRegistry()
    for descriptor in (
        _tool("web.http_client", "http_client"),
        _tool("web.proxy_replay", "proxy_replay"),
        _tool("web.operator_status", "operator_status"),
        _tool("recon.http_probe", "http_probe"),
        _tool("web.unhealthy_client", "unhealthy_client", health=ToolHealthStatus.UNHEALTHY),
    ):
        registry.register(descriptor)
    return registry


def test_catalog_returns_deterministic_candidates_for_canonical_action() -> None:
    catalog = MCPToolCatalog(
        _registry(),
        {
            "web.http_client": ("http_request",),
            "web.proxy_replay": ("http_request",),
            "web.operator_status": (),
            "recon.http_probe": ("service_discovery",),
            "web.unhealthy_client": ("http_request",),
        },
    )

    candidates = catalog.list_tools_for_action("web", "http_request")

    assert [candidate.tool_id for candidate in candidates] == [
        ToolId("web.http_client"),
        ToolId("web.proxy_replay"),
    ]
    assert catalog.supports(ToolId("web.proxy_replay"), "http_request")
    assert not catalog.supports(ToolId("web.operator_status"), "http_request")


def test_catalog_filters_namespace_and_unhealthy_tools() -> None:
    catalog = MCPToolCatalog(
        _registry(),
        {
            "web.http_client": ("service_discovery",),
            "web.proxy_replay": (),
            "web.operator_status": (),
            "recon.http_probe": ("service_discovery",),
            "web.unhealthy_client": ("service_discovery",),
        },
    )

    assert [
        tool.tool_id for tool in catalog.list_tools_for_action("web", "service_discovery")
    ] == [ToolId("web.http_client")]
    assert [
        tool.tool_id
        for tool in catalog.list_tools_for_action(
            "web", "service_discovery", include_unhealthy=True
        )
    ] == [ToolId("web.http_client"), ToolId("web.unhealthy_client")]


@pytest.mark.parametrize(
    ("capability", "operation"),
    [("WEB", "http_request"), ("web.api", "http_request"), ("web", "HTTP request")],
)
def test_catalog_rejects_noncanonical_action_names(
    capability: str, operation: str
) -> None:
    with pytest.raises(ValueError):
        MCPToolCatalog(_registry()).list_tools_for_action(capability, operation)


def test_catalog_rejects_bindings_for_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tools"):
        MCPToolCatalog(_registry(), {"web.not_registered": ("http_request",)})


def test_catalog_rejects_noncanonical_operation_binding() -> None:
    with pytest.raises(ValueError, match="noncanonical"):
        MCPToolCatalog(_registry(), {"web.http_client": ("HTTP Request",)})
