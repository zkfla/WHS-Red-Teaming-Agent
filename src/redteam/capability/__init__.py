"""Capability Layer. Owned by Capability Team.

Structure: ``ToolGateway`` -> ``MCPToolRegistry`` -> ``MCPAdapter`` ->
existing MCP Server. This subpackage does not implement new security
tools; it adapts existing MCP servers to
``redteam.core.ports.tool_gateway.ToolGateway``.
"""

from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry

__all__ = ["MCPToolCatalog", "MCPToolGateway", "MCPToolRegistry"]
