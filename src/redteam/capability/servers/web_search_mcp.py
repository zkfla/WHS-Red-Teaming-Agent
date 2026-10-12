"""Opt-in stdio MCP exposure for the bounded Tavily web search reader."""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from redteam.capability.servers.tavily_search import TavilySearchProvider
from redteam.capability.servers.web_search import (
    SearchConfig,
    SearchError,
    SearchProvider,
    SearchResult,
    WebSearchReader,
    parse_search_request,
)
from redteam.core.contracts.tool import PROVIDER_METADATA_RESOURCE_EFFECTS

REMOTE_TOOL_NAME = "web_search"
PROPOSED_OPERATION = "web_search"
PROFILE = "web_search_tavily"
MODULE = "redteam.capability.servers.web_search_mcp"


class _SearchMCPServer(MCPServer):
    """Keep the SDK's permissive argument model aligned with SearchRequest."""

    async def list_tools(self) -> list[Any]:
        tools = await super().list_tools()
        for tool in tools:
            if tool.name == REMOTE_TOOL_NAME:
                tool.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None,
    ) -> Any:
        if name == REMOTE_TOOL_NAME:
            try:
                parse_search_request(arguments)
            except SearchError as exc:
                raise ToolError(str(exc)) from None
        return await super().call_tool(name, arguments, context)


def load_web_search_config(
    path: str | Path, expected_sha256: str | None = None,
) -> tuple[SearchConfig, str]:
    """Pin one bounded operator configuration across discovery and server startup."""
    try:
        with Path(path).open("rb") as source:
            data = source.read(65537)
            if len(data) > 65536:
                raise ValueError("web search configuration exceeds 65536 bytes")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("web search configuration changed after bootstrap")
        return SearchConfig.model_validate_json(data), digest
    except (OSError, UnicodeError, ValueError):
        raise ValueError("invalid web search configuration") from None


def create_server(
    config: SearchConfig,
    *,
    provider: SearchProvider | None = None,
    api_key: str = "",
    prohibited_patterns: Sequence[str] = (),
) -> MCPServer:
    """Expose one search tool; construction-time settings never become tool arguments."""
    if provider is None:
        if config.external_search_enabled and not api_key:
            raise ValueError("TAVILY_API_KEY is required for enabled web search")
        provider = TavilySearchProvider(api_key=api_key, config=config)
    reader = WebSearchReader(provider, config, prohibited_patterns=prohibited_patterns)
    server = _SearchMCPServer("web-search", log_level="WARNING")

    async def web_search(query: str, max_results: int = min(5, config.max_results)) -> SearchResult:
        """Find public references; snippets are provider descriptions, not page text."""
        try:
            request = parse_search_request({"query": query, "max_results": max_results})
            return await reader.search(request)
        except SearchError as exc:
            raise ToolError(str(exc)) from None

    web_search.__annotations__["query"] = Annotated[str, Field(min_length=1, max_length=500)]
    web_search.__annotations__["max_results"] = Annotated[
        int, Field(strict=True, ge=1, le=config.max_results)
    ]
    server.tool(meta={PROVIDER_METADATA_RESOURCE_EFFECTS: []})(web_search)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    config, _ = load_web_search_config(args.config, args.sha256)
    create_server(config, api_key=os.environ.get("TAVILY_API_KEY", "")).run(transport="stdio")


if __name__ == "__main__":
    main()
