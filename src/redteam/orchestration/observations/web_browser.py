"""Map bounded browser navigation into an observation on an existing endpoint."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.target import AnyTarget, UrlTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType
from redteam.orchestration.observations.mapper import GraphMutation, NodeMutation


def map_web_browser_navigate(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Record successful navigation without copying page text or cookies.

    Playwright MCP versions differ in whether navigation is returned as
    structured content or text.  The framework only needs the fact that the
    approved observed URL was opened, so the mapper deliberately ignores the
    upstream payload and stores no page content.
    """

    if not result.success or not isinstance(target, UrlTarget):
        return GraphMutation()
    url = arguments.get("url")
    if not isinstance(url, str) or url != target.url:
        return GraphMutation()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or len(url) > 2048
    ):
        return GraphMutation()
    endpoint_id = node_id_for_target(target)
    observation = {
        "kind": "browser_navigation",
        "source_tool": "web.browser_navigate",
        "url": url,
        "navigation_completed": True,
        "page_content_omitted": True,
        "verdict": "observation_only",
    }
    return GraphMutation(
        nodes=[
            NodeMutation(
                GraphNodeType.ENDPOINT,
                endpoint_id,
                attrs={"browser_navigated": True},
                facts={
                    "observed_target": url,
                    "assessment_kind": "observation",
                },
                labels=["observed-by:web.browser_navigate"],
                observations=[observation],
            )
        ]
    )
