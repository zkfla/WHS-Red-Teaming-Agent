"""Agent-Team view of Tool Catalog without importing Capability code."""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import ToolId


class ToolCatalog(Protocol):
    """Structural subset already implemented by ``MCPToolGateway``/``MCPToolRegistry``."""

    def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]: ...

    def list_tools_for_action(self, capability: str, operation: str) -> list[ToolDescriptor]:
        """Eligible, healthy candidates bound to this canonical action.

        Mirrors ``redteam.core.ports.tool_gateway.ToolGateway.list_tools_for_action``
        — read-only, no selection policy. See
        ``docs/proposals/tool-selection-port.md``.
        """
        ...
