"""Tool Gateway port — the single entry point into the Capability Layer.

Only reachable with a ``ToolInvocation`` derived from an ALLOW
``PolicyDecision``. See ``redteam.core.ports.control_plane`` for the gate
in front of this.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from redteam.core.contracts.tool import ToolInvocation, ToolResult


class ToolGateway(Protocol):
    async def invoke(self, invocation: ToolInvocation) -> ToolResult: ...


BeforeToolStart = Callable[[], Awaitable[None]]


class ToolStartRejected(Exception):
    """The caller's current admission failed; this is not an adapter error."""


@runtime_checkable
class ToolStartAdmissionGateway(Protocol):
    async def invoke_with_admission(
        self,
        invocation: ToolInvocation,
        before_start: BeforeToolStart,
    ) -> ToolResult: ...
