"""Test-only stdio MCP child with a synthetic search provider; no network."""

from __future__ import annotations

import argparse

from redteam.capability.servers.web_search import ProviderSearchItem, ProviderSearchResponse
from redteam.capability.servers.web_search_mcp import create_server, load_web_search_config


class FakeProvider:
    provider_id = "tavily"

    async def search(self, query: str, max_results: int) -> ProviderSearchResponse:
        return ProviderSearchResponse(items=(
            ProviderSearchItem(
                title="Official synthetic reference",
                url="https://docs.example.org/reference",
                snippet="Synthetic\nsearch description",
            ),
        ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    config, _ = load_web_search_config(args.config, args.sha256)
    create_server(config, provider=FakeProvider()).run(transport="stdio")


if __name__ == "__main__":
    main()
