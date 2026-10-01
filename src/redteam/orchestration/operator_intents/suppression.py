"""Exact, read-only endpoint suppression shared by planning and replanning.

An operator path names an observed endpoint, not a string prefix or a new
target. URL selectors additionally bind the authority (and scheme for URL
node identities). This module never changes scope or calls a tool.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from redteam.attack_graph.identity import normalize_path, split_node_id
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId


def endpoint_excluded(node_id: GraphNodeId, selectors: list[str]) -> bool:
    """Check an endpoint identity against persisted, bounded operator selectors."""
    if not selectors:
        return False
    try:
        node_type, key = split_node_id(node_id)
    except ValueError:
        return False
    if node_type is not GraphNodeType.ENDPOINT:
        return False
    if "://" in key:
        parsed = urlsplit(key)
        node_authority = parsed.netloc.lower()
        node_scheme = parsed.scheme.lower()
        node_path = normalize_path(parsed.path).lower()
    else:
        parts = key.split(" ")
        node_authority = parts[0].lower() if len(parts) >= 3 else ""
        node_scheme = ""
        node_path = normalize_path(parts[-1]).lower()
    for selector in selectors:
        if not isinstance(selector, str) or not selector:
            continue
        if selector.startswith(("http://", "https://")):
            parsed_selector = urlsplit(selector)
            if node_scheme and node_scheme != parsed_selector.scheme.lower():
                continue
            if node_authority != parsed_selector.netloc.lower():
                continue
            path = parsed_selector.path
        elif selector.startswith("/"):
            path = selector
        else:
            continue
        if node_path == normalize_path(path).lower():
            return True
    return False
