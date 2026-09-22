"""Tool Gateway port — the single entry point into the Capability Layer.

Only reachable with a ``ToolInvocation`` derived from an ALLOW
``PolicyDecision``. See ``redteam.core.ports.control_plane`` for the gate
in front of this.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.tool import ToolInvocation, ToolResult


class ToolGateway(Protocol):
    async def invoke(self, invocation: ToolInvocation) -> ToolResult: ...
