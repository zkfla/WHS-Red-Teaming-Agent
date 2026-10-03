"""Canonical action to MCP ToolId candidate resolution tests."""

from __future__ import annotations

import pytest

from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry, ToolAliasConflictError
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

    assert [tool.tool_id for tool in catalog.list_tools_for_action("web", "service_discovery")] == [
        ToolId("web.http_client")
    ]
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
def test_catalog_rejects_noncanonical_action_names(capability: str, operation: str) -> None:
    with pytest.raises(ValueError):
        MCPToolCatalog(_registry()).list_tools_for_action(capability, operation)


def test_catalog_rejects_bindings_for_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tools"):
        MCPToolCatalog(_registry(), {"web.not_registered": ("http_request",)})


def test_catalog_rejects_noncanonical_operation_binding() -> None:
    with pytest.raises(ValueError, match="noncanonical"):
        MCPToolCatalog(
            _registry(),
            {
                "web.http_client": ("HTTP Request",),
                "web.proxy_replay": (),
                "web.operator_status": (),
                "recon.http_probe": ("service_discovery",),
                "web.unhealthy_client": ("http_request",),
            },
        )


def _physical_tool(server: str) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=build_canonical_mcp_tool_id("web", server, "request"),
        server_id=server,
        tool_name="request",
        capability_tags=["web"],
        operations=["http_request"],
        target_types=["host"],
        health_status=ToolHealthStatus.HEALTHY,
    )


def test_two_servers_with_same_remote_name_require_exact_physical_identity() -> None:
    first, second = _physical_tool("first.server"), _physical_tool("second.server")
    registry = MCPToolRegistry()
    registry.register(first)
    registry.register(second)
    catalog = MCPToolCatalog(registry)
    first_bound, second_bound = catalog.get(first.tool_id), catalog.get(second.tool_id)
    assert first_bound is not None and first_bound.server_id == first.server_id
    assert second_bound is not None and second_bound.server_id == second.server_id
    with pytest.raises(ToolAliasConflictError):
        catalog.get(ToolId("web.request"))
    with pytest.raises(ToolAliasConflictError):
        MCPToolCatalog(registry, {"web.request": ("http_request",)})
    # A fabricated canonical server must never fall back to another server.
    assert catalog.get(build_canonical_mcp_tool_id("web", "missing", "request")) is None


def test_legacy_metadata_aliases_bind_to_one_physical_tool_and_conflicts_fail() -> None:
    descriptor = _physical_tool("first.server")
    registry = MCPToolRegistry()
    registry.register(descriptor)
    schema = {"type": "object"}
    catalog = MCPToolCatalog(
        registry,
        {"web.request": ("http_request",)},
        output_schemas={"web.request": schema},
        target_types={"web.request": "host"},
    )
    bound = catalog.get(descriptor.tool_id)
    assert bound is not None
    assert bound.operations == ["http_request"]
    assert bound.output_schema == schema and bound.target_types == ["host"]
    with pytest.raises(ValueError, match="conflict for the same tool"):
        MCPToolCatalog(
            registry,
            {
                "web.request": ("http_request",),
                str(descriptor.tool_id): ("page_read",),
            },
        )
