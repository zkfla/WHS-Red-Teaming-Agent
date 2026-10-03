"""In-memory MCP Tool Registry. Owned by Capability Team.

Responsibility boundary with ``MCPToolCatalog`` (``capability/catalog.py``):
this Registry is the authoritative store of *what was discovered* --
plain ``register``/``get``/``all``/``list_by_capability``/health bookkeeping,
with no operation-binding or candidate-metadata policy of its own. Which
canonical ``capability.operation`` a discovered tool actually answers to
(``list_tools_for_action``, ``list_candidate_metadata``) is a bound,
per-Gateway *view* over one discovery snapshot -- ``MCPToolCatalog``'s job,
not duplicated here. A tool's discovery snapshot legitimately needs both:
this class does not know or care about operation bindings; the Catalog
never itself stores a tool.
"""

from __future__ import annotations

from redteam.capability.adapters.mcp.naming import (
    is_canonical_mcp_tool_id,
    legacy_alias_for_tool_id,
    parse_canonical_mcp_tool_id,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId


class ToolRegistrationConflictError(ValueError):
    """Raised instead of silently overwriting a ToolId owned by another tool."""


class ToolAliasConflictError(ValueError):
    """One legacy 2-part id is claimed by tools from several servers."""


def legacy_alias_for_descriptor(descriptor: ToolDescriptor) -> str | None:
    """Legacy alias for one registered descriptor, or None when unmappable."""

    return legacy_alias_for_tool_id(descriptor.tool_id)


class MCPToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[ToolId, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        current = self._tools.get(descriptor.tool_id)
        if current is not None and current != descriptor:
            raise ToolRegistrationConflictError(
                f"tool id {descriptor.tool_id} is already registered by {current.server_id}"
            )
        self._tools[descriptor.tool_id] = descriptor.model_copy(deep=True)

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        descriptor = self._tools.get(tool_id)
        return descriptor.model_copy(deep=True) if descriptor is not None else None

    def resolve(self, tool_id: ToolId) -> ToolDescriptor | None:
        """Canonical-first single resolver over the discovery snapshot."""

        query = ToolId(str(tool_id))
        if is_canonical_mcp_tool_id(query):
            capability, server_id, remote_name = parse_canonical_mcp_tool_id(query)
            matching = [
                item
                for item in self._tools.values()
                if item.server_id == server_id
                and item.tool_name == remote_name
                and capability in item.capability_tags
            ]
            if len(matching) > 1:
                raise ToolAliasConflictError("duplicate physical tool binding")
            return matching[0].model_copy(deep=True) if matching else None
        query_alias = legacy_alias_for_tool_id(query)
        matches = (
            [
                descriptor
                for descriptor in self._tools.values()
                if legacy_alias_for_descriptor(descriptor) == query_alias
            ]
            if query_alias is not None
            else []
        )
        exact = self._tools.get(query)
        if exact is not None:
            if is_canonical_mcp_tool_id(query):
                return exact.model_copy(deep=True)
            others = [item for item in matches if item.tool_id != query]
            if others:
                owners = sorted({str(item.server_id) for item in (exact, *others)})
                raise ToolAliasConflictError(
                    f"tool id {query} is claimed by several servers: "
                    + ", ".join(owners)
                    + "; use the canonical 3-part tool id"
                )
            return exact.model_copy(deep=True)
        if len(matches) == 1:
            return matches[0].model_copy(deep=True)
        if len(matches) > 1:
            owners = sorted({str(item.server_id) for item in matches})
            raise ToolAliasConflictError(
                f"legacy tool alias {query} matches tools from several servers: "
                + ", ".join(owners)
                + "; use the canonical 3-part tool id"
            )
        return None

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return [
            tool.model_copy(deep=True)
            for tool in self._sorted_tools()
            if capability_tag in tool.capability_tags
        ]

    def list_by_server(self, server_id: str) -> list[ToolDescriptor]:
        return [
            tool.model_copy(deep=True)
            for tool in self._sorted_tools()
            if tool.server_id == server_id
        ]

    def all(self) -> list[ToolDescriptor]:
        return [tool.model_copy(deep=True) for tool in self._sorted_tools()]

    def replace_server_tools(self, server_id: str, descriptors: list[ToolDescriptor]) -> None:
        """Atomically replace the discovery snapshot for one MCP server."""

        replacements: dict[ToolId, ToolDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.server_id != server_id:
                raise ValueError(
                    f"descriptor {descriptor.tool_id} belongs to {descriptor.server_id}, "
                    f"expected {server_id}"
                )
            if descriptor.tool_id in replacements:
                raise ToolRegistrationConflictError(
                    f"duplicate tool id {descriptor.tool_id} in discovery result"
                )
            existing = self._tools.get(descriptor.tool_id)
            if existing is not None and existing.server_id != server_id:
                raise ToolRegistrationConflictError(
                    f"tool id {descriptor.tool_id} is already registered by {existing.server_id}"
                )
            replacements[descriptor.tool_id] = descriptor.model_copy(deep=True)

        retained = {
            tool_id: descriptor
            for tool_id, descriptor in self._tools.items()
            if descriptor.server_id != server_id
        }
        self._tools = {**retained, **replacements}

    def update_server_health(self, server_id: str, status: ToolHealthStatus) -> bool:
        """Refresh the health status of every tool owned by one MCP server.

        Returns ``True`` when at least one descriptor actually changed. An
        unknown server is a no-op so a stale server list can never mutate the
        Registry. Health is the only field this method touches — it is a
        liveness signal, never an authorization decision.
        """

        changed = False
        for tool_id, descriptor in tuple(self._tools.items()):
            if descriptor.server_id != server_id:
                continue
            if descriptor.health_status == status:
                continue
            self._tools[tool_id] = descriptor.model_copy(update={"health_status": status})
            changed = True
        return changed

    def server_health(self, server_id: str) -> ToolHealthStatus | None:
        """Current health for one server, or ``None`` when it owns no tools.

        The server's descriptors are refreshed in unison, so the first matching
        descriptor is representative.
        """

        for descriptor in self._sorted_tools():
            if descriptor.server_id == server_id:
                return descriptor.health_status
        return None

    def _sorted_tools(self) -> list[ToolDescriptor]:
        return sorted(self._tools.values(), key=lambda descriptor: str(descriptor.tool_id))
