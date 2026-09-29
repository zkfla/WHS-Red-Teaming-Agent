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

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId


class ToolRegistrationConflictError(ValueError):
    """Raised instead of silently overwriting a ToolId owned by another tool."""


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

    def replace_server_tools(
        self, server_id: str, descriptors: list[ToolDescriptor]
    ) -> None:
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
