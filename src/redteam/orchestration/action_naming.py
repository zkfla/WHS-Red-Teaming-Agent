"""Compatibility exports for shared intent naming, plus ToolId helpers.

An action key describes intent (``capability.operation``).  A ``ToolId``
describes an execution mechanism and is deliberately validated separately.
"""

from __future__ import annotations

from redteam.core.contracts.action_naming import (
    CANONICAL_CAPABILITIES,
    CANONICAL_OPERATIONS,
    canonical_action_key,
)
from redteam.core.ids import ToolId

__all__ = [
    "CANONICAL_CAPABILITIES",
    "CANONICAL_OPERATIONS",
    "canonical_action_key",
    "tool_namespace",
    "tool_matches_action_namespace",
]


def tool_namespace(tool_id: ToolId) -> str:
    """Return the namespace from ``<namespace>.<remote_tool_name>``."""

    namespace, separator, remote_name = str(tool_id).partition(".")
    if not separator or not namespace or not remote_name or "." in remote_name:
        raise ValueError(f"tool_id {tool_id!r} must use <namespace>.<remote_tool_name>")
    return namespace


def tool_matches_action_namespace(tool_id: ToolId, capability: str) -> bool:
    """Fail closed when a bound ToolId belongs to another capability."""

    try:
        return tool_namespace(tool_id) == capability
    except ValueError:
        return False
