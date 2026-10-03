"""Inspect one public Tavily search through the internal search reader."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from time import perf_counter

from redteam.capability.servers.tavily_search import TavilySearchProvider
from redteam.capability.servers.web_search import (
    SearchConfig,
    SearchError,
    WebSearchReader,
    parse_search_request,
)


async def _search(query: str, api_key: str) -> int:
    config = SearchConfig(external_search_enabled=True, max_results=5)
    reader = WebSearchReader(TavilySearchProvider(api_key=api_key, config=config), config)
    started = perf_counter()
    try:
        result = await reader.search(parse_search_request({"query": query}))
    except SearchError as exc:
        print(f"Search failed: {exc.code.value}", file=sys.stderr)
        return 1

    print(f"Elapsed: {perf_counter() - started:.2f}s")
    if not result.items:
        print("No results")
    for index, item in enumerate(result.items, start=1):
        print(f"\n{index}. {item.title or '(no title)'}")
        print(f"URL: {item.url}")
        print(f"Description: {item.snippet or '(no description)'}")
    if result.truncated:
        print("Results truncated by the local limits")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="one public, non-sensitive search query")
    args = parser.parse_args()
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        print("TAVILY_API_KEY is not set", file=sys.stderr)
        return 2
    return asyncio.run(_search(args.query, api_key))


if __name__ == "__main__":
    raise SystemExit(main())
