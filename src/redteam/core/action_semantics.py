"""Shared, tool-independent action meanings used at team boundaries.

New meanings are explicit Core changes. Remote tools and model responses
cannot add to this vocabulary.
"""

from __future__ import annotations

import re

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
    """Validate an action meaning and return its exact ROE identity."""
    if capability not in CANONICAL_CAPABILITIES:
        raise ValueError(f"capability {capability!r} is not a canonical v1 namespace")
    if not _LOWER_SNAKE_CASE.fullmatch(operation):
        raise ValueError(f"operation {operation!r} must be lower_snake_case")
    if operation not in CANONICAL_OPERATIONS:
        raise ValueError(f"operation {operation!r} is not in the canonical v1 contract")
    return f"{capability}.{operation}"
