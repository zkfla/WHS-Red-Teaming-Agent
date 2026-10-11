"""Read-only card/detail projection over the single descriptor source.

This module owns no registry and stores no tools. Every card is built from
a caller-supplied ToolDescriptor snapshot so filtered views can never become
a hidden allowlist and no second registry can drift from discovery.

Search always reads the full descriptor fields; the truncated card summary
is display text only. Capability filtering is an exact tag match, the
same rule as registry list_by_capability.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ports.tool_catalog import (
    ToolCard,
    ToolCardKind,
    ToolDetail,
    filter_tool_cards,
)

CARD_SUMMARY_CHARS = 240


def tool_card_from_descriptor(
    descriptor: ToolDescriptor,
    *,
    kind: ToolCardKind = "mcp_tool",
    owner_scope: str = "mcp",
) -> ToolCard:
    """Project one descriptor to its display card.

    The summary is truncated to CARD_SUMMARY_CHARS characters for display;
    search must read the descriptor fields, never this text. Kind and owner
    describe which single source snapshot the card was projected from; they
    are display labels, never execution branching inputs.
    """

    summary = (descriptor.description or "").strip()
    if len(summary) > CARD_SUMMARY_CHARS:
        summary = summary[:CARD_SUMMARY_CHARS].rstrip() + "..."
    return ToolCard(
        tool_id=descriptor.tool_id,
        server_id=descriptor.server_id,
        tool_name=descriptor.tool_name,
        kind=kind,
        owner_scope=owner_scope,
        title=descriptor.tool_name,
        summary=summary,
        capability_tags=list(descriptor.capability_tags),
        risk_level=descriptor.risk_level,
        health_status=descriptor.health_status,
        callable=True,
    )


def tool_detail_from_descriptor(descriptor: ToolDescriptor) -> ToolDetail:
    return ToolDetail(
        card=tool_card_from_descriptor(descriptor),
        description=descriptor.description or "",
        input_schema=deepcopy(descriptor.input_schema),
        output_schema=deepcopy(descriptor.output_schema),
        target_types=list(descriptor.target_types),
        operations=list(descriptor.operations),
        required_arguments=list(descriptor.required_arguments),
        optional_arguments=list(descriptor.optional_arguments),
    )


def cards_from_descriptors(
    descriptors: Sequence[ToolDescriptor],
    *,
    kind: ToolCardKind | None = None,
    capability: str | None = None,
    query: str | None = None,
    include_unhealthy: bool = False,
) -> list[ToolCard]:
    """Build display cards from one descriptor snapshot.

    Capability is an exact tag match, the same rule as
    MCPToolRegistry.list_by_capability. Query is split on whitespace and
    every token must appear somewhere in the tool id, tool name, server
    id, full description, capability tags, operations, or target types.
    An unknown kind raises ValueError; known kinds with no cards simply
    match nothing.
    """

    entries = [
        (
            tool_card_from_descriptor(descriptor),
            " ".join(
                [
                    str(descriptor.tool_id),
                    descriptor.tool_name,
                    descriptor.server_id,
                    descriptor.description or "",
                    *descriptor.capability_tags,
                    *descriptor.operations,
                    *descriptor.target_types,
                ]
            ),
        )
        for descriptor in descriptors
    ]
    return filter_tool_cards(
        entries,
        kind=kind,
        capability=capability,
        query=query,
        include_unhealthy=include_unhealthy,
    )
