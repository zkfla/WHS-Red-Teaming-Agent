"""Compatibility exports for shared intent naming, plus ToolId helpers.

An action key describes intent (``capability.operation``).  A ``ToolId``
describes an execution mechanism and is deliberately validated separately.
"""

from __future__ import annotations

from redteam.core.action_semantics import (
    CANONICAL_CAPABILITIES,
    CANONICAL_OPERATIONS,
    canonical_action_key,
)
from redteam.core.ids import ToolId

# Intent validation lives in redteam.core.action_semantics (single source of
# truth); this module re-exports it so existing import paths keep working.

__all__ = [
    "CANONICAL_CAPABILITIES",
    "CANONICAL_OPERATIONS",
    "canonical_action_key",
    "tool_namespace",
    "tool_matches_action_namespace",
]


def tool_namespace(tool_id: ToolId) -> str:
    """Read the namespace from legacy or physical tool IDs.

    Physical identity and escaping are checked by the capability catalog;
    orchestration consumes its namespace without duplicating that parser.
    """
    parts = str(tool_id).split(".")
    if len(parts) not in (2, 3) or not all(parts):
        raise ValueError(f"tool_id {tool_id!r} must use a legacy or physical tool identity")
    return parts[0]


def tool_matches_action_namespace(tool_id: ToolId, capability: str) -> bool:
    """Fail closed when a bound ToolId belongs to another capability."""

    try:
        return tool_namespace(tool_id) == capability
    except ValueError:
        return False
