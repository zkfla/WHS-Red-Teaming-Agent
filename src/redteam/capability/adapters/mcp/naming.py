"""Deterministic names for MCP tools exposed to the framework."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

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

    return ToolId(f"{normalize_tool_segment(namespace)}.{normalize_tool_segment(remote_tool_name)}")


def escape_tool_segment(value: str) -> str:
    """Percent-escape one id segment, preserving the exact original text."""

    if not isinstance(value, str) or not value:
        raise ValueError("tool id segment must not be empty")
    return quote(value, safe="").replace(".", "%2E")


def unescape_tool_segment(value: str) -> str:
    """Invert escape_tool_segment; malformed input raises ValueError."""

    if not isinstance(value, str) or not value:
        raise ValueError("tool id segment must not be empty")
    if "." in value:
        raise ValueError("tool id segment must not contain an unescaped separator: " + repr(value))
    decoded = unquote(value, errors="strict")
    if escape_tool_segment(decoded) != value:
        raise ValueError("tool id segment must use canonical escaping")
    return decoded


def build_canonical_mcp_tool_id(capability: str, server_id: str, remote_tool_name: str) -> ToolId:
    """Return the canonical 3-part id for one remote tool."""

    return ToolId(
        normalize_tool_segment(capability)
        + "."
        + escape_tool_segment(server_id)
        + "."
        + escape_tool_segment(remote_tool_name)
    )


def parse_canonical_mcp_tool_id(tool_id: ToolId | str) -> tuple[str, str, str]:
    """Split a canonical id into (capability, server_id, remote_tool_name)."""

    parts = str(tool_id).split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "canonical tool id must use <capability>.<server>.<remote> format: " + repr(tool_id)
        )
    capability, server, remote = parts
    if normalize_tool_segment(capability) != capability:
        raise ValueError("canonical tool id has a non-normalized capability: " + repr(tool_id))
    return capability, unescape_tool_segment(server), unescape_tool_segment(remote)


def is_canonical_mcp_tool_id(value: object) -> bool:
    """Whether value parses as a canonical 3-part tool id."""

    if not isinstance(value, str):
        return False
    try:
        parse_canonical_mcp_tool_id(value)
    except ValueError:
        return False
    return True


def legacy_alias_for_tool_id(value: ToolId | str) -> str | None:
    """Legacy 2-part alias for one tool id, or None when unmappable."""

    text = str(value)
    parts = text.split(".")
    if len(parts) == 2 and all(parts):
        return text
    if len(parts) == 3 and all(parts):
        try:
            capability, _server_id, remote = parse_canonical_mcp_tool_id(text)
            return capability + "." + normalize_tool_segment(remote)
        except ValueError:
            return None
    return None
