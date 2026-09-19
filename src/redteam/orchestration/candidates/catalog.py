"""Agent-Team view of Tool Catalog without importing Capability code."""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import ToolId


class ToolCatalog(Protocol):
    """Structural subset already implemented by ``MCPToolRegistry``."""

    def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]: ...
