"""Dummy/harmless example MCP adapter.

Stands in for a real MCP server so the end-to-end flow (Agent ->
Control Plane -> Capability -> Evidence -> Event -> Attack Graph) can be
exercised without any real security tool wired up. Per instruction.md
section 12/21: no real exploit tooling is added at this stage.

TODO(capability-team): replace/extend with adapters for real, chosen
MCP servers once selected (see docs/adr/0003-mcp-capability.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ActionId, InvocationId, ToolId

EXAMPLE_TOOL_ID = ToolId("recon.ping")


class ExampleMCPAdapter:
    """Talks to ``deploy/mcp`` example server in real deployments.

    Runs fully in-process here (no network call) so the skeleton and
    tests don't depend on the container being up.
    """

    server_id = "example-mcp"

    async def discover_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                tool_id=EXAMPLE_TOOL_ID,
                server_id=self.server_id,
                tool_name="ping",
                description="Harmless example tool: echoes arguments back.",
                input_schema={"type": "object", "properties": {"message": {"type": "string"}}},
                capability_tags=["recon"],
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.HEALTHY,
            )
        ]

    async def invoke(self, tool: ToolDescriptor, arguments: dict) -> ToolResult:
        started_at = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=InvocationId(""),  # overwritten by MCPToolGateway
            action_id=ActionId(""),  # overwritten by MCPToolGateway
            tool_id=tool.tool_id,
            success=True,
            output={"echo": arguments, "server_id": self.server_id},
            error=None,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            metadata={"tool_name": tool.tool_name},
        )

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY
