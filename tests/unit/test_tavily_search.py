"""Tavily adapter tests use HTTPX's in-memory transport only."""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import traceback
from collections.abc import AsyncIterator

import httpx
import pytest

from examples import tavily_search_quality
from redteam.capability.servers.tavily_search import TavilySearchProvider
from redteam.capability.servers.web_search import (
    SearchConfig,
    SearchError,
    SearchErrorCode,
    SearchRequest,
    WebSearchReader,
)


def _enabled(**changes: object) -> SearchConfig:
    return SearchConfig.model_validate({"external_search_enabled": True, **changes})


def _provider(
    handler: httpx.MockTransport, *, config: SearchConfig | None = None, key: str = "test-key",
    max_response_bytes: int = 262144,
) -> TavilySearchProvider:
    return TavilySearchProvider(
        api_key=key, config=config or _enabled(), transport=handler,
        max_response_bytes=max_response_bytes,
    )


def test_quality_cli_preserves_full_query_and_result_item_pairs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = "OWASP API Security BOLA official documentation"
    key = "test-only-key"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "results": [
                {"title": "Bola Person", "url": "https://example.org/people/bola",
                 "content": "An unrelated person", "score": 0.8},
                {"title": "Bola Game", "url": "https://example.org/games/bola",
                 "content": "An unrelated game", "score": 0.4},
            ],
            "answer": "raw-response-only-marker",
        })

    transport = httpx.MockTransport(handle)

    def provider_factory(api_key: str, config: SearchConfig) -> TavilySearchProvider:
        assert api_key == key
        return TavilySearchProvider(api_key=api_key, config=config, transport=transport)

    monkeypatch.setattr(tavily_search_quality, "TavilySearchProvider", provider_factory)
    monkeypatch.setattr(sys, "argv", ["tavily_search_quality.py", query])
    monkeypatch.setenv("TAVILY_API_KEY", key)
    assert tavily_search_quality.main() == 0

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert json.loads(request.content) == {
        "query": query,
        "max_results": 5,
        "search_depth": "basic",
        "auto_parameters": False,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    output = capsys.readouterr()
    assert output.err == ""
    assert "1. Bola Person\nURL: https://example.org/people/bola\n" in output.out
    assert "Description: An unrelated person" in output.out
    assert "2. Bola Game\nURL: https://example.org/games/bola\n" in output.out
    assert "Description: An unrelated game" in output.out
    assert "raw-response-only-marker" not in output.out + output.err + caplog.text
    assert key not in output.out + output.err + caplog.text
    assert "Authorization" not in output.out + output.err + caplog.text
    assert request.headers["Authorization"] == f"Bearer {key}"


def test_quality_cli_sensitive_query_never_reaches_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handle)

    def provider_factory(api_key: str, config: SearchConfig) -> TavilySearchProvider:
        return TavilySearchProvider(api_key=api_key, config=config, transport=transport)

    monkeypatch.setattr(tavily_search_quality, "TavilySearchProvider", provider_factory)
    monkeypatch.setattr(sys, "argv", ["tavily_search_quality.py", "Bearer private-query-value"])
    monkeypatch.setenv("TAVILY_API_KEY", "test-only-key")
    assert tavily_search_quality.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip() == "Search failed: sensitive_query"
    assert calls == 0


async def test_request_is_fixed_and_response_maps_only_supplied_fields() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "OWASP", "url": "https://owasp.org/docs", "content": "API guidance",
             "score": 0.97, "published_date": "yesterday"},
            {"url": "https://owasp.org/other"},
        ]})

    config = _enabled(max_results=2)
    provider = _provider(httpx.MockTransport(handle), config=config)
    result = await WebSearchReader(provider, config).search(SearchRequest(
        query="  OWASP API  ", max_results=10
    ))
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "query": "OWASP API", "max_results": 2, "search_depth": "basic",
        "auto_parameters": False, "include_answer": False,
        "include_raw_content": False, "include_images": False,
    }
    assert result.provider == "tavily"
    assert len(result.items) == 2
    assert result.items[0].title == "OWASP"
    assert result.items[0].snippet == "API guidance"
    assert result.items[0].source is None
    assert result.items[1].title is None
    assert result.items[1].snippet is None
    assert result.items[1].source is None
    assert "score" not in result.model_dump_json()
    assert "published_date" not in result.model_dump_json()


async def test_empty_results_are_success() -> None:
    provider = _provider(httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})))
    result = await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
    assert result.items == () and not result.truncated


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"results":{}}',
                                  b'{"results":[{"url": 12}]}'])
async def test_invalid_json_or_shape_is_safe_error(body: bytes) -> None:
    provider = _provider(httpx.MockTransport(lambda _: httpx.Response(
        200, content=body, headers={"Content-Type": "application/json"}
    )))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
    assert failure.value.code is SearchErrorCode.INVALID_PROVIDER_RESPONSE


@pytest.mark.parametrize("status,code", [
    (401, SearchErrorCode.AUTHENTICATION_FAILED),
    (429, SearchErrorCode.RATE_LIMITED),
    (432, SearchErrorCode.CREDITS_EXHAUSTED),
    (433, SearchErrorCode.CREDITS_EXHAUSTED),
    (400, SearchErrorCode.PROVIDER_ERROR),
    (422, SearchErrorCode.PROVIDER_ERROR),
    (500, SearchErrorCode.PROVIDER_ERROR),
    (302, SearchErrorCode.PROVIDER_ERROR),
])
async def test_status_mapping_never_exposes_remote_error(
    status: int, code: SearchErrorCode
) -> None:
    secret = "private-upstream-body"
    provider = _provider(httpx.MockTransport(
        lambda _: httpx.Response(status, text=secret, headers={"Location": "https://evil.test"})
    ))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
    assert failure.value.code is code
    assert secret not in "".join(traceback.format_exception(failure.value))


async def test_bounded_stream_rejects_inaccurate_or_missing_content_length() -> None:
    for headers in ({}, {"Content-Length": "1"}):
        calls = 0

        def handle(_: httpx.Request, response_headers: dict[str, str] = headers) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200, headers={"Content-Type": "application/json", **response_headers},
                content=b"x" * 300,
            )

        provider = _provider(httpx.MockTransport(handle), max_response_bytes=128)
        with pytest.raises(SearchError) as failure:
            await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
        assert failure.value.code is SearchErrorCode.PROVIDER_RESPONSE_TOO_LARGE
        assert calls == 1


async def test_compressed_response_is_bounded_after_decoding() -> None:
    compressed = gzip.compress(b"x" * 300)
    assert len(compressed) < 128
    provider = _provider(httpx.MockTransport(lambda _: httpx.Response(
        200, content=compressed,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )), max_response_bytes=128)
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
    assert failure.value.code is SearchErrorCode.PROVIDER_RESPONSE_TOO_LARGE


async def test_invalid_compression_is_invalid_response() -> None:
    provider = _provider(httpx.MockTransport(lambda _: httpx.Response(
        200, content=b"not-gzip",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
    assert failure.value.code is SearchErrorCode.INVALID_PROVIDER_RESPONSE


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, block: bool = False) -> None:
        self.body = body
        self.block = block
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        yield self.body
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("case", ["success", "error", "too_large", "timeout", "cancel"])
async def test_response_stream_closes_on_all_outcomes(case: str) -> None:
    stream = _TrackedStream(
        b'{"results":[]}' if case == "success" else b"x" * 300,
        block=case in {"timeout", "cancel"},
    )

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500 if case == "error" else 200,
            headers={"Content-Type": "application/json"}, stream=stream,
        )

    config = _enabled(timeout_seconds=0.01 if case == "timeout" else 10)
    provider = _provider(httpx.MockTransport(handle), config=config, max_response_bytes=128)
    reader = WebSearchReader(provider, config)
    if case == "cancel":
        task = asyncio.create_task(reader.search(SearchRequest(query="public docs")))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif case == "success":
        assert (await reader.search(SearchRequest(query="public docs"))).items == ()
    else:
        with pytest.raises(SearchError):
            await reader.search(SearchRequest(query="public docs"))
    assert stream.closed


async def test_disabled_sensitive_or_missing_key_never_sends_request() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handle)
    with pytest.raises(SearchError) as disabled:
        await WebSearchReader(_provider(transport), SearchConfig()).search(
            SearchRequest(query="public docs")
        )
    assert disabled.value.code is SearchErrorCode.EXTERNAL_SEARCH_DISABLED
    with pytest.raises(SearchError) as sensitive:
        await WebSearchReader(_provider(transport), _enabled()).search(
            SearchRequest(query="Authorization: Bearer private-value")
        )
    assert sensitive.value.code is SearchErrorCode.SENSITIVE_QUERY
    with pytest.raises(SearchError) as missing:
        await WebSearchReader(_provider(transport, key=""), _enabled()).search(
            SearchRequest(query="public docs")
        )
    assert missing.value.code is SearchErrorCode.AUTHENTICATION_FAILED
    assert calls == 0


async def test_timeout_and_cancellation_remain_distinct() -> None:
    started = asyncio.Event()

    async def handle(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"results": []})

    config = _enabled(timeout_seconds=0.01)
    provider = _provider(httpx.MockTransport(handle), config=config)
    with pytest.raises(SearchError) as timed_out:
        await WebSearchReader(provider, config).search(SearchRequest(query="public docs"))
    assert timed_out.value.code is SearchErrorCode.TIMEOUT

    task = asyncio.create_task(WebSearchReader(provider, _enabled()).search(
        SearchRequest(query="public docs")
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_network_error_is_safe_and_not_retried() -> None:
    calls = 0
    secret = "private-provider-key"

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(secret, request=request)

    provider = _provider(httpx.MockTransport(handle), key=secret)
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, _enabled()).search(SearchRequest(query="public docs"))
    assert failure.value.code is SearchErrorCode.NETWORK_ERROR
    assert secret not in "".join(traceback.format_exception(failure.value))
    assert calls == 1
