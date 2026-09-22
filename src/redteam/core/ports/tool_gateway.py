"""Tool Gateway port — the single entry point into the Capability Layer.

Only reachable with a ``ToolInvocation`` derived from an ALLOW
``PolicyDecision``. See ``redteam.core.ports.control_plane`` for the gate
in front of this.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.tool import ToolDescriptor, ToolInvocation, ToolResult


class ToolGateway(Protocol):
    async def invoke(self, invocation: ToolInvocation) -> ToolResult: ...

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
    ) -> list[ToolDescriptor]:
        """Read-only Registry candidates bound to this canonical action.

        Selection policy stays with Orchestration: this performs no remote
        call, executes nothing, and makes no policy decision — it only
        answers "which registered tools are eligible" (namespace match,
        operator-configured operation binding, current health). See
        ``docs/proposals/tool-selection-port.md``.
        """
        ...
