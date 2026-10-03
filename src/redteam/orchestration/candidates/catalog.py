"""Compatibility import for the Agent team's canonical Tool Catalog port.

The orchestration layer may consume this name for backwards compatibility,
but the contract is owned by Core.  Keeping a second Protocol here made it
easy for Agent code and Capability code to drift apart.
"""

from redteam.core.action_semantics import ActionNamingError, canonical_action_key
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ports.tool_catalog import ToolCatalog


def bound_operations(catalog: ToolCatalog, tool: ToolDescriptor, capability: str) -> list[str]:
    """Offer only common intents whose live binding agrees with lookup metadata."""
    if capability not in tool.capability_tags or catalog.get(tool.tool_id) != tool:
        return []
    if tool.health_status is ToolHealthStatus.UNHEALTHY:
        return []
    operations = []
    for operation in tool.operations:
        try:
            canonical_action_key(capability, operation)
        except ActionNamingError:
            continue
        if tool in catalog.list_tools_for_action(capability, operation):
            operations.append(operation)
    return operations


__all__ = ["ToolCatalog"]
