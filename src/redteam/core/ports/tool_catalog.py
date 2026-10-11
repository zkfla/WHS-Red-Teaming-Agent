"""Read-only Capability catalog port used by Agent/Orchestration.

The port deliberately returns metadata only. It never executes a tool and
never decides whether a proposed action is allowed; those responsibilities
remain with Control Plane and ToolGateway respectively.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, Field

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId

#: Card source vocabulary. mcp_tool cards come from this catalog descriptor
#: snapshot; the other values are reserved for future owners (local helpers,
#: shell program cards, reference bundles) reusing the same shape without a
#: second registry. Never branch central execution logic on these strings.
ToolCardKind = Literal["mcp_tool", "local_helper", "program", "reference"]


class ToolCard(BaseModel):
    tool_id: ToolId
    server_id: str
    tool_name: str
    kind: ToolCardKind = "mcp_tool"
    owner_scope: str = "mcp"
    title: str = ""
    summary: str = ""
    capability_tags: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    health_status: ToolHealthStatus = ToolHealthStatus.UNKNOWN
    # Kind-level flag, not liveness: True for callable kinds ("mcp_tool"),
    # False for non-callable kinds. Live availability is health_status;
    # health is never duplicated into this flag.
    callable: bool = True


class ToolDetail(BaseModel):
    card: ToolCard
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    target_types: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    required_arguments: list[str] = Field(default_factory=list)
    optional_arguments: list[str] = Field(default_factory=list)


def validate_card_kind(kind: ToolCardKind | None) -> None:
    """Reject an unknown card kind before any filtering runs.

    Known kinds with no cards simply match nothing; an unknown kind is a
    caller bug, never an empty result.
    """

    if kind is not None and kind not in get_args(ToolCardKind):
        raise ValueError(f"unknown tool card kind: {kind!r}")


def card_query_tokens(query: str | None) -> tuple[str, ...]:
    """Whitespace-split lowercase tokens; every token must match."""

    return tuple((query or "").lower().split())


def filter_tool_cards(
    entries: Sequence[tuple[ToolCard, str]],
    *,
    kind: ToolCardKind | None = None,
    capability: str | None = None,
    query: str | None = None,
    include_unhealthy: bool = False,
) -> list[ToolCard]:
    """Display-only filter over card plus full-text haystack pairs.

    Each entry carries its display card and the full source text the query
    reads (never the truncated card summary). Capability is an exact tag
    match against the card tags. Every query token must appear somewhere in
    the haystack. Cards are display only: filtering never removes anything
    from the underlying source snapshot, and a filtered view is never an
    execution allowlist.
    """

    validate_card_kind(kind)
    tokens = card_query_tokens(query)
    kept: list[ToolCard] = []
    for card, haystack in entries:
        if capability is not None and capability not in card.capability_tags:
            continue
        if not include_unhealthy and card.health_status == ToolHealthStatus.UNHEALTHY:
            continue
        if tokens and not all(token in haystack.lower() for token in tokens):
            continue
        if kind is not None and card.kind != kind:
            continue
        kept.append(card)
    return sorted(kept, key=lambda item: str(item.tool_id))


class ToolCatalog(Protocol):
    def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...

    def all_descriptors(self) -> list[ToolDescriptor]:
        """Every registered tool as a finalized descriptor.

        Role views filter this list without consulting operation
        bindings, so mapper-less tools stay visible and ops-empty
        tools are still listed.
        """
        ...

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]: ...

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
    ) -> list[ToolDescriptor]: ...


class ToolCardView(Protocol):
    """Narrow read-only card projection port, separate from ToolCatalog.

    Broad catalog consumers (risk resolution, runtime deps) must not be
    forced to implement the card surface. The concrete catalog and gateway
    satisfy both protocols; a future model-context consumer depends on this
    narrow one.

    Cards are display only: filters never remove tools from the catalog
    itself. Callable names the callable kind and never reports live
    availability; check health_status for that.
    """

    def list_cards(
        self,
        *,
        kind: ToolCardKind | None = None,
        capability: str | None = None,
        query: str | None = None,
        include_unhealthy: bool = False,
    ) -> list[ToolCard]:
        """Display cards over the catalog snapshot.

        Capability is an exact tag match, the same rule as
        ToolCatalog.list_by_capability. Query is split on whitespace and
        every token must appear in the full descriptor fields. An unknown
        kind raises ValueError.
        """
        ...

    def get_detail(self, tool_id: ToolId) -> ToolDetail | None: ...
