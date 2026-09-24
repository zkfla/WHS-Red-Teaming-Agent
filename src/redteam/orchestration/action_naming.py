"""Canonical ActionProposal naming owned by the orchestration boundary.

An action key describes intent (``capability.operation``).  A ``ToolId``
describes an execution mechanism and is deliberately validated separately.
"""

from __future__ import annotations

import re

from redteam.core.ids import ToolId

CANONICAL_CAPABILITIES = frozenset({"recon", "web", "mobile", "analysis"})
CANONICAL_OPERATIONS = frozenset(
    {
        "port_scan",
        "service_discovery",
        "endpoint_discovery",
        "http_request",
        "auth_attempt",
        "parameter_fuzz",
        "file_upload",
        "app_launch",
        "device_shell",
        "runtime_instrumentation",
        "static_analysis",
        "response_analysis",
    }
)
_LOWER_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def canonical_action_key(capability: str, operation: str) -> str:
    """Validate and return the ROE key ``<capability>.<operation>``."""

    if capability not in CANONICAL_CAPABILITIES:
        raise ValueError(
            f"capability {capability!r} is not a canonical v1 namespace"
        )
    if not _LOWER_SNAKE_CASE.fullmatch(operation):
        raise ValueError(f"operation {operation!r} must be lower_snake_case")
    if operation not in CANONICAL_OPERATIONS:
        raise ValueError(f"operation {operation!r} is not in the canonical v1 contract")
    return f"{capability}.{operation}"


def tool_namespace(tool_id: ToolId) -> str:
    """Return the namespace from ``<namespace>.<remote_tool_name>``."""

    namespace, separator, remote_name = str(tool_id).partition(".")
    if not separator or not namespace or not remote_name or "." in remote_name:
        raise ValueError(
            f"tool_id {tool_id!r} must use <namespace>.<remote_tool_name>"
        )
    return namespace


def tool_matches_action_namespace(tool_id: ToolId, capability: str) -> bool:
    """Fail closed when a bound ToolId belongs to another capability."""

    try:
        return tool_namespace(tool_id) == capability
    except ValueError:
        return False
