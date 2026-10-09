"""``DummyToolGateway`` — test double for ``redteam.core.ports.tool_gateway.ToolGateway``.

NOT a production implementation — see ``tests/fakes/__init__.py``. The
real gateway is ``redteam.capability.gateway.MCPToolGateway``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.capability.card_view import (
    cards_from_descriptors,
    tool_detail_from_descriptor,
)
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult
from redteam.core.ports.tool_catalog import ToolCard, ToolCardKind, ToolDetail


class DummyToolGateway:
    """Returns a canned ``ToolResult`` for every invocation — no
    registry, no adapters, no real dispatch.

    ``tools`` optionally seeds a static, deterministic ``list_tools_for_action``
    result set for tests exercising Tool Selection without a real
    ``MCPToolGateway``/Registry.
    """

    def __init__(
        self,
        success: bool = True,
        output: dict[str, Any] | None = None,
        *,
        tools: list[ToolDescriptor] | None = None,
    ) -> None:
        self.success = success
        self.output = output if output is not None else {}
        self.invocations: list[ToolInvocation] = []
        self._tools = list(tools or [])

    def list_tools_for_action(self, capability: str, operation: str) -> list[ToolDescriptor]:
        return [
            tool
            for tool in self._tools
            if capability in tool.capability_tags and operation in tool.operations
        ]

    def get(self, tool_id: Any) -> ToolDescriptor | None:
        """Structural ``ToolCatalog`` support -- lets a test pass ``self``
        as both ``tool_gateway`` and ``tool_catalog`` to exercise real
        Action-aware Tool Selection without a full ``MCPToolGateway``."""
        return next((tool for tool in self._tools if tool.tool_id == tool_id), None)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return [tool for tool in self._tools if capability_tag in tool.capability_tags]

    def all_descriptors(self) -> list[ToolDescriptor]:
        return list(self._tools)

    def list_cards(
        self,
        *,
        kind: ToolCardKind | None = None,
        capability: str | None = None,
        query: str | None = None,
        include_unhealthy: bool = False,
    ) -> list[ToolCard]:
        return cards_from_descriptors(
            self._tools,
            kind=kind,
            capability=capability,
            query=query,
            include_unhealthy=include_unhealthy,
        )

    def get_detail(self, tool_id: Any) -> ToolDetail | None:
        descriptor = self.get(tool_id)
        if descriptor is None:
            return None
        return tool_detail_from_descriptor(descriptor)

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
                None if self.success else ToolError(code="dummy_failure", message="forced failure")
            ),
            started_at=now,
            finished_at=now,
        )
