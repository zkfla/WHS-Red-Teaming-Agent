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
from typing import Any

from redteam.capability.action_binding import declares_retry_safe
from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId

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
                output_schema={
                    "type": "object",
                    "properties": {"echo": {"type": "object"}, "server_id": {"type": "string"}},
                    "required": ["echo", "server_id"],
                },
                operations=["service_discovery"],
                target_types=["host"],
                capability_tags=["recon"],
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.HEALTHY,
                retry_safe=declares_retry_safe("recon", ("service_discovery",)),
            )
        ]

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        started_at = datetime.now(timezone.utc)
        return MCPCallResult(
            success=True,
            output={"echo": arguments, "server_id": self.server_id},
            error=None,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            metadata={"tool_name": tool.tool_name},
        )

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY
