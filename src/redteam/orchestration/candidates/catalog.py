"""Compatibility import for the Agent team's canonical Tool Catalog port.

The orchestration layer may consume this name for backwards compatibility,
but the contract is owned by Core.  Keeping a second Protocol here made it
easy for Agent code and Capability code to drift apart.
"""

from redteam.core.ports.tool_catalog import ToolCatalog

__all__ = ["ToolCatalog"]
