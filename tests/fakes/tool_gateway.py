"""``DummyToolGateway`` — test double for ``redteam.core.ports.tool_gateway.ToolGateway``.

NOT a production implementation — see ``tests/fakes/__init__.py``. The
real gateway is ``redteam.capability.gateway.MCPToolGateway``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.tool import ToolError, ToolInvocation, ToolResult


class DummyToolGateway:
    """Returns a canned ``ToolResult`` for every invocation — no
    registry, no adapters, no real dispatch."""

    def __init__(self, success: bool = True, output: dict[str, Any] | None = None) -> None:
        self.success = success
        self.output = output if output is not None else {}
        self.invocations: list[ToolInvocation] = []

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        now = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=self.success,
            output=self.output,
            error=(
                None
                if self.success
                else ToolError(code="dummy_failure", message="forced failure")
            ),
            started_at=now,
            finished_at=now,
        )
