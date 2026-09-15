"""Deterministic names for MCP tools exposed to the framework."""

from __future__ import annotations

import re

from redteam.core.ids import ToolId

_INVALID_SEGMENT = re.compile(r"[^a-z0-9_-]+")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def normalize_tool_segment(value: str) -> str:
    """Normalize one ToolId segment without leaking MCP-specific syntax."""

    normalized = _INVALID_SEGMENT.sub("_", value.strip().lower())
    normalized = _REPEATED_UNDERSCORE.sub("_", normalized).strip("_-")
    if not normalized:
        raise ValueError("tool id segment must contain an alphanumeric character")
    return normalized


def build_mcp_tool_id(namespace: str, remote_tool_name: str) -> ToolId:
    """Return ``<capability-namespace>.<normalized-remote-name>``."""

    return ToolId(
        f"{normalize_tool_segment(namespace)}.{normalize_tool_segment(remote_tool_name)}"
    )
