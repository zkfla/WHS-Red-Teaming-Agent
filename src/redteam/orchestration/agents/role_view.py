"""Runtime role views for orchestration (assembled snapshots only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from redteam.core.contracts.role_selection import (
    resolve_injected_selection,
    selection_allows,
)

if TYPE_CHECKING:
    from redteam.core.ports.tool_catalog import ToolCatalog


def resolve_role_selection(definition, manifest=None):
    """Injected snapshot first, else supplied manifest, else capabilities."""

    return resolve_injected_selection(definition, manifest)


def role_tool_visible(definition, tool, manifest=None):
    """Whether one tool is visible to one role. Verbatim ids, no aliases."""

    selection = resolve_injected_selection(definition, manifest)
    return selection_allows(
        selection,
        tool_id=str(tool.tool_id),
        server_id=str(tool.server_id),
        capability_tags=list(tool.capability_tags),
    )


def tools_for_role(definition, catalog: ToolCatalog | None = None, manifest=None):
    """Visible descriptors for one role over the existing catalog port."""

    if catalog is None:
        return []
    selection = resolve_injected_selection(definition, manifest)
    visible = []
    for descriptor in catalog.all_descriptors():
        if selection_allows(
            selection,
            tool_id=str(descriptor.tool_id),
            server_id=str(descriptor.server_id),
            capability_tags=list(descriptor.capability_tags),
        ):
            visible.append(descriptor)
    return visible
