"""Data-only tool double for the explicit central development selection.

No transport, subprocess, credential, callback or network client is accepted.
The composition root accepts this exact type rather than arbitrary providers.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult


class DevelopmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    output: Any = None
    error: ToolError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DevelopmentToolGateway:
    def __init__(self, descriptors: list[ToolDescriptor], responses: list[DevelopmentResponse]):
        registry = MCPToolRegistry()
        for descriptor in descriptors:
            registry.register(descriptor)
        self.catalog = MCPToolCatalog(registry)
        self.responses = [item.model_copy(deep=True) for item in responses]
        self.invocations: list[ToolInvocation] = []
        # Tests may suspend a call to inspect committed intermediate state.
        self.started = asyncio.Event()
        self.calls_changed = asyncio.Condition()
        self.call_releases: dict[str, asyncio.Event] = {}
        self.release = asyncio.Event()
        self.release.set()

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        if self.catalog.get(invocation.tool_id) is None:
            raise ValueError("development_tool_not_registered")
        async with self.calls_changed:
            self.invocations.append(invocation.model_copy(deep=True))
            call_release = asyncio.Event()
            call_release.set()
            self.call_releases[str(invocation.invocation_id)] = call_release
            self.calls_changed.notify_all()
        self.started.set()
        if not self.responses:
            raise ValueError("development_response_exhausted")
        response = self.responses.pop(0)
        await self.release.wait()
        await call_release.wait()
        now = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            started_at=now,
            finished_at=now,
            success=response.success,
            output=response.output,
            error=response.error,
            metadata={**response.metadata, "mock": True, "development_double": True},
        )

    async def wait_for_invocations(self, count: int):
        async with self.calls_changed:
            await self.calls_changed.wait_for(lambda: len(self.invocations) >= count)
