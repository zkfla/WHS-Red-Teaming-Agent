"""Read-only Capability catalog port used by Agent/Orchestration.

The port deliberately returns metadata only. It never executes a tool and
never decides whether a proposed action is allowed; those responsibilities
remain with Control Plane and ToolGateway respectively.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import ToolId


class ToolCatalog(Protocol):
    def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]: ...

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
    ) -> list[ToolDescriptor]: ...
