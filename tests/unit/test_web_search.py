"""Provider-neutral web search tests; no network or search account is used."""

from __future__ import annotations

import asyncio
import socket
import traceback
from typing import cast

import pytest
from pydantic import ValidationError

from redteam.capability.servers.web_search import (
    ProviderFailure,
    ProviderSearchItem,
    ProviderSearchResponse,
    SearchConfig,
    SearchError,
    SearchErrorCode,
    SearchRequest,
    WebSearchReader,
    parse_search_request,
)


class FakeProvider:
    provider_id = "fake_public_search"

    def __init__(
        self,
        response: ProviderSearchResponse | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.response = response or ProviderSearchResponse()
        self.failure = failure
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, max_results: int) -> ProviderSearchResponse:
        self.calls.append((query, max_results))
        if self.failure is not None:
            raise self.failure
        return self.response


def enabled(**kwargs: object) -> SearchConfig:
    return SearchConfig.model_validate({"external_search_enabled": True, **kwargs})


def item(**kwargs: str | None) -> ProviderSearchItem:
    return ProviderSearchItem(url="https://docs.example.org/guide", **kwargs)


async def test_normal_search_preserves_only_supplied_fields_without_visiting_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_dns(*args: object, **kwargs: object) -> None:
        raise AssertionError("search result URL must not be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    provider = FakeProvider(ProviderSearchResponse(items=(item(),)))
    result = await WebSearchReader(provider, enabled()).search(
        SearchRequest(query="  public docs  ")
    )
    assert provider.calls == [("public docs", 5)]
    assert result.provider == "fake_public_search"
    offset = result.retrieved_at.utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    assert result.items[0].url == "https://docs.example.org/guide"
    assert result.items[0].title is None
    assert result.items[0].snippet is None
    assert result.items[0].source is None
    assert not result.truncated
    assert "public docs" not in result.model_dump_json()


async def test_empty_result_is_success() -> None:
    result = await WebSearchReader(FakeProvider(), enabled()).search(SearchRequest(query="docs"))
    assert result.items == () and result.truncated is False


@pytest.mark.parametrize(
    "data",
    [
        {"query": "   "},
        {"query": "word\nsecret"},
        {"query": "word\u2028secret"},
        {"query": "x" * 501},
        {"query": "docs", "max_results": 0},
        {"query": "docs", "max_results": 11},
        {"query": "docs", "unexpected": "value"},
    ],
)
def test_request_rejects_invalid_input(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(data)
    with pytest.raises(SearchError) as failure:
        parse_search_request(data)
    assert failure.value.code is SearchErrorCode.INVALID_INPUT
    assert str(data) not in str(failure.value)


def test_config_rejects_invalid_limits() -> None:
    for field, value in (
        ("max_results", 11),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("max_title_chars", 1001),
        ("max_snippet_chars", 5001),
        ("max_output_bytes", 127),
    ):
        with pytest.raises(ValidationError):
            SearchConfig.model_validate({field: value})


async def test_disabled_and_sensitive_queries_never_reach_provider() -> None:
    provider = FakeProvider()
    with pytest.raises(SearchError) as disabled:
        await WebSearchReader(provider, SearchConfig()).search(SearchRequest(query="docs"))
    assert disabled.value.code is SearchErrorCode.EXTERNAL_SEARCH_DISABLED
    for query in (
        "Authorization: Bearer private-value",
        "Bearer private-value",
        "Cookie: session=private-value",
        "password=private-value",
        "token: private-value",
        "api_key=private-value",
    ):
        with pytest.raises(SearchError) as refused:
            await WebSearchReader(provider, enabled()).search(SearchRequest(query=query))
        assert refused.value.code is SearchErrorCode.SENSITIVE_QUERY
        assert query not in str(refused.value)
        assert "private-value" not in str(refused.value)
    assert provider.calls == []


async def test_wrong_reader_input_has_invalid_input_code() -> None:
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(FakeProvider(), enabled()).search(cast(SearchRequest, {"query": "x"}))
    assert failure.value.code is SearchErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "code",
    [
        SearchErrorCode.AUTHENTICATION_FAILED,
        SearchErrorCode.RATE_LIMITED,
        SearchErrorCode.NETWORK_ERROR,
        SearchErrorCode.INVALID_PROVIDER_RESPONSE,
        SearchErrorCode.PROVIDER_ERROR,
    ],
)
async def test_provider_failures_have_distinct_safe_codes(code: SearchErrorCode) -> None:
    provider = FakeProvider(failure=ProviderFailure(code))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, enabled()).search(SearchRequest(query="docs"))
    assert failure.value.code is code


async def test_upstream_exception_text_is_not_exposed_or_chained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-provider-key"
    provider = FakeProvider(failure=RuntimeError(secret))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, enabled()).search(SearchRequest(query="docs"))
    assert failure.value.code is SearchErrorCode.PROVIDER_ERROR
    assert secret not in str(failure.value)
    assert secret not in "".join(traceback.format_exception(failure.value))
    assert failure.value.__context__ is None
    assert secret not in caplog.text


async def test_timeout_and_external_cancellation_remain_distinct() -> None:
    class WaitingProvider(FakeProvider):
        async def search(self, query: str, max_results: int) -> ProviderSearchResponse:
            self.calls.append((query, max_results))
            await asyncio.Event().wait()
            return ProviderSearchResponse()

    provider = WaitingProvider()
    with pytest.raises(SearchError) as timeout:
        await WebSearchReader(provider, enabled(timeout_seconds=0.01)).search(
            SearchRequest(query="docs")
        )
    assert timeout.value.code is SearchErrorCode.TIMEOUT
    task = asyncio.create_task(
        WebSearchReader(provider, enabled()).search(SearchRequest(query="docs"))
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "url",
    [
        "ftp://docs.example.org/guide",
        "https://name:secret@docs.example.org/guide",
        "https://docs.example.org:99999/guide",
        "https://docs.example.org/has space",
        "https://docs.example.org/%wrong",
        "https://docs.example.org/line\nbreak",
        "https://bad^host/guide",
        "https://-bad.example.org/guide",
    ],
)
async def test_bad_provider_url_is_response_error(url: str) -> None:
    provider = FakeProvider(ProviderSearchResponse(items=(ProviderSearchItem(url=url),)))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, enabled()).search(SearchRequest(query="docs"))
    assert failure.value.code is SearchErrorCode.INVALID_PROVIDER_RESPONSE
    assert url not in str(failure.value)


async def test_wrong_provider_response_type_is_rejected() -> None:
    provider = FakeProvider()
    provider.response = cast(ProviderSearchResponse, {"items": []})
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, enabled()).search(SearchRequest(query="docs"))
    assert failure.value.code is SearchErrorCode.INVALID_PROVIDER_RESPONSE


async def test_server_result_limit_and_text_truncation() -> None:
    provider = FakeProvider(
        ProviderSearchResponse(
            items=(
                item(title="A" * 12, snippet="B" * 15, source="Official"),
                item(title="second"),
                item(title="third"),
            )
        )
    )
    result = await WebSearchReader(
        provider, enabled(max_results=2, max_title_chars=5, max_snippet_chars=7)
    ).search(SearchRequest(query="docs", max_results=10))
    assert provider.calls == [("docs", 2)]
    assert len(result.items) == 2
    assert result.items[0].title == "AAAAA"
    assert result.items[0].snippet == "BBBBBBB"
    assert result.items[0].source == "Official"
    assert result.truncated


async def test_utf8_json_byte_limit_removes_whole_items() -> None:
    response = ProviderSearchResponse(items=(item(title="한글" * 20), item(title="두번째" * 20)))
    full = await WebSearchReader(FakeProvider(response), enabled()).search(
        SearchRequest(query="docs")
    )
    full_bytes = len(full.model_dump_json().encode("utf-8"))
    bounded = await WebSearchReader(
        FakeProvider(response), enabled(max_output_bytes=max(128, full_bytes - 1))
    ).search(SearchRequest(query="docs"))
    assert len(bounded.model_dump_json().encode("utf-8")) <= max(128, full_bytes - 1)
    assert len(bounded.items) < 2
    assert bounded.truncated
    assert all(result.url == "https://docs.example.org/guide" for result in bounded.items)


async def test_unrepresentable_empty_envelope_is_output_too_large() -> None:
    provider = FakeProvider()
    provider.provider_id = "p" * 64
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, enabled(max_output_bytes=128)).search(
            SearchRequest(query="docs")
        )
    assert failure.value.code is SearchErrorCode.OUTPUT_TOO_LARGE


async def test_invalid_provider_text_is_rejected() -> None:
    provider = FakeProvider(ProviderSearchResponse(items=(item(snippet="bad\x00text"),)))
    with pytest.raises(SearchError) as failure:
        await WebSearchReader(provider, enabled()).search(SearchRequest(query="docs"))
    assert failure.value.code is SearchErrorCode.INVALID_PROVIDER_RESPONSE


def test_invalid_query_validation_message_hides_input() -> None:
    secret = "private-secret"
    with pytest.raises(ValidationError) as failure:
        SearchRequest(query=f"{secret}\n")
    assert secret not in str(failure.value)
