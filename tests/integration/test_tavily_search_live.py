"""Opt-in Tavily account check; never called by ordinary pytest runs."""

from __future__ import annotations

import os

import pytest

from redteam.capability.servers.tavily_search import TavilySearchProvider
from redteam.capability.servers.web_search import (
    SearchConfig,
    SearchRequest,
    WebSearchReader,
)


async def test_tavily_search_live_once() -> None:
    if os.environ.get("REDTEAM_TAVILY_LIVE") != "1":
        pytest.skip("live Tavily test requires REDTEAM_TAVILY_LIVE=1")
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        pytest.skip("live Tavily test requires TAVILY_API_KEY")

    config = SearchConfig(external_search_enabled=True)
    provider = TavilySearchProvider(api_key=api_key, config=config)
    result = await WebSearchReader(provider, config).search(SearchRequest(
        query="OWASP API Security BOLA official documentation", max_results=5
    ))
    assert result.provider == "tavily"
    offset = result.retrieved_at.utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    assert len(result.items) <= 5
    assert len(result.model_dump_json().encode("utf-8")) <= config.max_output_bytes
    assert all(item.url.startswith(("http://", "https://")) for item in result.items)
