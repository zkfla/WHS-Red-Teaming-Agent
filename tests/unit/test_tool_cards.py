"""Card/detail lookup over the single descriptor source (TASK-004).

Cards are a display projection: the unfiltered view must list every
registered tool, filtered views must never shrink the registry view, and
search reads the full descriptor while the summary stays truncated.
"""

from __future__ import annotations

import pytest

from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId
from redteam.core.ports.tool_catalog import ToolCard
from tests.fakes.tool_gateway import DummyToolGateway

# Appears only past the 240-char display truncation point, so a search hit
# proves the query read the full descriptor instead of the card summary.
NEEDLE = "needle-xyz-beyond-truncation"


def _tool(
    tool_id: str,
    tool_name: str,
    *,
    capability: str,
    description: str = "",
    operations: list[str] | None = None,
    target_types: list[str] | None = None,
    health: ToolHealthStatus = ToolHealthStatus.HEALTHY,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(tool_id),
        server_id="server-" + tool_name,
        tool_name=tool_name,
        description=description,
        capability_tags=[capability],
        operations=list(operations or []),
        target_types=list(target_types or []),
        health_status=health,
    )


def _ids(cards: list[ToolCard]) -> set[str]:
    return {str(card.tool_id) for card in cards}


def _registry() -> MCPToolRegistry:
    registry = MCPToolRegistry()
    for descriptor in (
        _tool(
            "web.http_client",
            "http_client",
            capability="web",
            description="H" * 250 + " " + NEEDLE,
            operations=["http_request"],
        ),
        _tool(
            "web.operator_status",
            "operator_status",
            capability="web",
            description="Reports operator status.",
            health=ToolHealthStatus.UNKNOWN,
        ),
        _tool(
            "recon.http_probe",
            "http_probe",
            capability="recon",
            description="Single GET probe.",
            operations=["service_discovery"],
            target_types=["host"],
        ),
        _tool(
            "web.unhealthy_client",
            "unhealthy_client",
            capability="web",
            description="Unhealthy client.",
            operations=["http_request"],
            health=ToolHealthStatus.UNHEALTHY,
        ),
    ):
        registry.register(descriptor)
    return registry


def _catalog() -> MCPToolCatalog:
    return MCPToolCatalog(_registry())


def test_default_cards_include_unknown_health_but_hide_unhealthy() -> None:
    catalog = _catalog()
    cards = catalog.list_cards()
    assert _ids(cards) == {
        "recon.http_probe",
        "web.http_client",
        "web.operator_status",
    }
    assert all(card.kind == "mcp_tool" and card.callable for card in cards)


def test_include_unhealthy_reveals_hidden_tool() -> None:
    catalog = _catalog()
    assert "web.unhealthy_client" not in _ids(catalog.list_cards())
    assert "web.unhealthy_client" in _ids(catalog.list_cards(include_unhealthy=True))


def test_query_finds_keyword_beyond_summary_truncation() -> None:
    catalog = _catalog()
    summaries = {str(card.tool_id): card.summary for card in catalog.list_cards()}
    assert NEEDLE not in summaries["web.http_client"]
    assert _ids(catalog.list_cards(query=NEEDLE)) == {"web.http_client"}


def test_query_matches_operation_server_id_and_target_type() -> None:
    catalog = _catalog()
    assert _ids(catalog.list_cards(query="service_discovery")) == {"recon.http_probe"}
    assert _ids(catalog.list_cards(query="server-http_probe")) == {"recon.http_probe"}
    assert _ids(catalog.list_cards(query="host")) == {"recon.http_probe"}


def test_multi_word_query_requires_every_token() -> None:
    catalog = _catalog()
    assert _ids(catalog.list_cards(query="probe single")) == {"recon.http_probe"}
    assert catalog.list_cards(query="probe missing-token") == []


def test_card_filters_are_display_only() -> None:
    catalog = _catalog()
    assert _ids(catalog.list_cards(capability="recon")) == {"recon.http_probe"}
    assert _ids(catalog.list_cards(kind="program")) == set()
    assert catalog.get(ToolId("web.http_client")) is not None
    assert "web.http_client" in {str(item.tool_id) for item in catalog.all_descriptors()}


def test_detail_returns_full_description_and_operations() -> None:
    catalog = _catalog()
    detail = catalog.get_detail(ToolId("web.http_client"))
    assert detail is not None
    assert NEEDLE in detail.description
    assert detail.operations == ["http_request"]
    assert catalog.get_detail(ToolId("web.unknown")) is None


def test_unhealthy_tool_detail_still_available() -> None:
    catalog = _catalog()
    detail = catalog.get_detail(ToolId("web.unhealthy_client"))
    assert detail is not None
    assert detail.description == "Unhealthy client."
    assert detail.operations == ["http_request"]


def test_unknown_kind_raises() -> None:
    catalog = _catalog()
    gateway = MCPToolGateway(_registry(), {})
    dummy = DummyToolGateway(tools=_registry().all())
    with pytest.raises(ValueError):
        catalog.list_cards(kind="mcp-tools")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        gateway.list_cards(kind="mcp-tools")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dummy.list_cards(kind="mcp-tools")  # type: ignore[arg-type]


def test_gateway_card_lookup_reads_full_descriptors() -> None:
    registry = _registry()
    gateway = MCPToolGateway(registry, {})
    assert _ids(gateway.list_cards()) == {
        "recon.http_probe",
        "web.http_client",
        "web.operator_status",
    }
    assert _ids(gateway.list_cards(query=NEEDLE)) == {"web.http_client"}
    detail = gateway.get_detail(ToolId("recon.http_probe"))
    assert detail is not None
    assert detail.description == "Single GET probe."
    assert detail.operations == ["service_discovery"]
    assert gateway.get_detail(ToolId("recon.unknown")) is None


def test_dummy_gateway_shares_card_search_semantics() -> None:
    dummy = DummyToolGateway(tools=_registry().all())
    assert _ids(dummy.list_cards()) == {
        "recon.http_probe",
        "web.http_client",
        "web.operator_status",
    }
    assert _ids(dummy.list_cards(query=NEEDLE)) == {"web.http_client"}
    assert "web.unhealthy_client" in _ids(dummy.list_cards(include_unhealthy=True))


def test_new_registration_appears_in_cards() -> None:
    registry = _registry()
    catalog = MCPToolCatalog(registry)
    assert "web.new_tool" not in _ids(catalog.list_cards())
    registry.register(_tool("web.new_tool", "new_tool", capability="web"))
    assert "web.new_tool" in _ids(catalog.list_cards())
