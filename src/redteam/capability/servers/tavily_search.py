"""Fixed-options Tavily Search adapter for the internal WebSearchReader."""

from __future__ import annotations

import asyncio

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from redteam.capability.servers.web_search import (
    ProviderFailure,
    ProviderSearchItem,
    ProviderSearchResponse,
    SearchConfig,
    SearchErrorCode,
)

_ENDPOINT = "https://api.tavily.com/search"
_DEFAULT_MAX_RESPONSE_BYTES = 262144
_MAX_RESPONSE_BYTES = 1048576


class _TavilyItem(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    url: str = Field(strict=True)
    title: str | None = Field(default=None, strict=True)
    content: str | None = Field(default=None, strict=True)


class _TavilyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    results: list[_TavilyItem]


def _status_error(status: int) -> SearchErrorCode:
    if status == 401:
        return SearchErrorCode.AUTHENTICATION_FAILED
    if status == 429:
        return SearchErrorCode.RATE_LIMITED
    if status in {432, 433}:
        return SearchErrorCode.CREDITS_EXHAUSTED
    return SearchErrorCode.PROVIDER_ERROR


async def _read_bounded(response: httpx.Response, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    # aiter_bytes yields decoded chunks, so a compressed response is bounded
    # after decompression. Content-Length is deliberately not trusted.
    async for chunk in response.aiter_bytes(chunk_size=8192):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class TavilySearchProvider:
    """One isolated HTTPS request per call; no SDK, retries, cookies, or env proxy."""

    provider_id = "tavily"

    def __init__(
        self,
        api_key: str,
        config: SearchConfig,
        *,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("invalid provider response byte limit")
        self._api_key = api_key
        self._config = config
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def search(self, query: str, max_results: int) -> ProviderSearchResponse:
        if not isinstance(self._api_key, str) or not self._api_key or any(
            char.isspace() or ord(char) < 33 or ord(char) > 126 for char in self._api_key
        ):
            raise ProviderFailure(SearchErrorCode.AUTHENTICATION_FAILED)
        if not isinstance(max_results, int) or not 1 <= max_results <= self._config.max_results:
            raise ProviderFailure(SearchErrorCode.PROVIDER_ERROR)

        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "auto_parameters": False,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        failure: SearchErrorCode | None = None
        body: bytes | None = None
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self._config.timeout_seconds,
            ) as client:
                async with client.stream(
                    "POST", _ENDPOINT, headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        failure = _status_error(response.status_code)
                    elif (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        != "application/json"
                    ):
                        failure = SearchErrorCode.INVALID_PROVIDER_RESPONSE
                    else:
                        body = await _read_bounded(response, self._max_response_bytes)
                        if body is None:
                            failure = SearchErrorCode.PROVIDER_RESPONSE_TOO_LARGE
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            failure = SearchErrorCode.TIMEOUT
        except httpx.DecodingError:
            failure = SearchErrorCode.INVALID_PROVIDER_RESPONSE
        except httpx.RequestError:
            failure = SearchErrorCode.NETWORK_ERROR
        except Exception:
            failure = SearchErrorCode.PROVIDER_ERROR

        # Raising after an except block prevents upstream exceptions, which may
        # contain credentials or query text, from becoming public __context__.
        if failure is not None:
            raise ProviderFailure(failure)
        if body is None:
            raise ProviderFailure(SearchErrorCode.INVALID_PROVIDER_RESPONSE)
        try:
            response_data = _TavilyResponse.model_validate_json(body)
            return ProviderSearchResponse(items=tuple(
                ProviderSearchItem(url=item.url, title=item.title, snippet=item.content)
                for item in response_data.results
            ))
        except (ValidationError, ValueError):
            pass
        raise ProviderFailure(SearchErrorCode.INVALID_PROVIDER_RESPONSE)
