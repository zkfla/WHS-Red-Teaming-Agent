"""Provider-neutral public search preparation and result normalization.

This internal service neither connects to a search provider nor registers an MCP tool.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from redteam.core.outbound_text import check_outbound_text


class SearchErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    EXTERNAL_SEARCH_DISABLED = "external_search_disabled"
    SENSITIVE_QUERY = "sensitive_query"
    AUTHENTICATION_FAILED = "authentication_failed"
    RATE_LIMITED = "rate_limited"
    CREDITS_EXHAUSTED = "credits_exhausted"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    OUTPUT_TOO_LARGE = "output_too_large"
    PROVIDER_RESPONSE_TOO_LARGE = "provider_response_too_large"
    PROVIDER_ERROR = "provider_error"


class SearchError(ValueError):
    """Stable public error with no query, URL, or upstream exception text."""

    def __init__(self, code: SearchErrorCode) -> None:
        self.code = code
        super().__init__("web_search: " + code.value)


class ProviderFailure(Exception):
    """Safe classification that a future provider adapter may raise."""

    def __init__(self, code: SearchErrorCode) -> None:
        if code not in {
            SearchErrorCode.AUTHENTICATION_FAILED,
            SearchErrorCode.RATE_LIMITED,
            SearchErrorCode.CREDITS_EXHAUSTED,
            SearchErrorCode.TIMEOUT,
            SearchErrorCode.NETWORK_ERROR,
            SearchErrorCode.INVALID_PROVIDER_RESPONSE,
            SearchErrorCode.PROVIDER_RESPONSE_TOO_LARGE,
            SearchErrorCode.PROVIDER_ERROR,
        }:
            raise ValueError("invalid provider failure code")
        self.code = code
        super().__init__(code.value)


class _LocalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


class SearchRequest(_LocalModel):
    query: str = Field(min_length=1, max_length=500, strict=True, repr=False)
    max_results: int = Field(default=5, ge=1, le=10, strict=True)

    @field_validator("query", mode="before")
    @classmethod
    def validate_query(cls, value: object) -> str:
        if not isinstance(value, str) or _has_control(value):
            raise ValueError("invalid search query")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("invalid search query")
        return cleaned


def parse_search_request(data: Mapping[str, object]) -> SearchRequest:
    """Turn untrusted input into a typed request without leaking validation input."""
    try:
        return SearchRequest.model_validate(dict(data))
    except (ValidationError, TypeError, ValueError):
        pass
    raise SearchError(SearchErrorCode.INVALID_INPUT)


class SearchConfig(_LocalModel):
    external_search_enabled: bool = Field(default=False, strict=True)
    max_results: int = Field(default=10, ge=1, le=10, strict=True)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)
    max_title_chars: int = Field(default=300, ge=1, le=1000, strict=True)
    max_snippet_chars: int = Field(default=1000, ge=1, le=5000, strict=True)
    max_output_bytes: int = Field(default=65536, ge=128, le=1048576, strict=True)


class ProviderSearchItem(_LocalModel):
    title: str | None = Field(default=None, max_length=10000, repr=False)
    url: str = Field(min_length=1, max_length=8192, repr=False)
    snippet: str | None = Field(default=None, max_length=50000, repr=False)
    source: str | None = Field(default=None, max_length=300, repr=False)


class ProviderSearchResponse(_LocalModel):
    items: tuple[ProviderSearchItem, ...] = Field(default=(), max_length=100)


class SearchItem(_LocalModel):
    title: str | None = Field(default=None, repr=False)
    url: str = Field(repr=False)
    snippet: str | None = Field(default=None, repr=False)
    source: str | None = Field(default=None, repr=False)


class SearchResult(_LocalModel):
    provider: str
    retrieved_at: datetime
    items: tuple[SearchItem, ...]
    truncated: bool


class SearchProvider(Protocol):
    provider_id: str

    async def search(self, query: str, max_results: int) -> ProviderSearchResponse: ...


# Narrow outbound-text expressions live in redteam.core.outbound_text now;
# this module keeps no private copy so every layer shares one meaning.
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _provider_url(value: str) -> str:
    if (
        _has_control(value)
        or "\\" in value
        or _BAD_ESCAPE.search(value)
        or any(char.isspace() for char in value)
    ):
        raise SearchError(SearchErrorCode.INVALID_PROVIDER_RESPONSE)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.netloc
            or parsed.netloc.endswith(":")
            or ".." in parsed.hostname
        ):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        url = httpx.URL(value)
        if not url.host:
            raise ValueError
        ascii_host = url.raw_host.decode("ascii").lower().rstrip(".")
        try:
            ipaddress.ip_address(ascii_host)
        except ValueError:
            if (
                len(ascii_host) > 253
                or not ascii_host
                or any(not _DNS_LABEL.fullmatch(label) for label in ascii_host.split("."))
            ):
                raise ValueError from None
        return str(url)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise SearchError(SearchErrorCode.INVALID_PROVIDER_RESPONSE) from None


def _text(value: str | None, limit: int | None = None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if _has_control(value):
        raise SearchError(SearchErrorCode.INVALID_PROVIDER_RESPONSE)
    cleaned = value.strip()
    if not cleaned:
        return None, False
    if limit is not None and len(cleaned) > limit:
        return cleaned[:limit], True
    return cleaned, False


def _snippet_text(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    # Search excerpts commonly include line breaks. Fold only ordinary line
    # whitespace, then let _text reject every other prohibited control.
    return _text(re.sub(r"[ \t\r\n]+", " ", value), limit)


def _normalize(
    response: ProviderSearchResponse, request: SearchRequest, config: SearchConfig, provider_id: str
) -> SearchResult:
    items: list[SearchItem] = []
    text_truncated = False
    for raw in response.items:
        url = _provider_url(raw.url)
        title, title_cut = _text(raw.title, config.max_title_chars)
        snippet, snippet_cut = _snippet_text(raw.snippet, config.max_snippet_chars)
        source, _ = _text(raw.source)
        text_truncated |= title_cut or snippet_cut
        items.append(SearchItem(title=title, url=url, snippet=snippet, source=source))
    limit = min(request.max_results, config.max_results)
    omitted = len(items) > limit
    items = items[:limit]
    retrieved_at = datetime.now(timezone.utc)
    while True:
        result = SearchResult(
            provider=provider_id,
            retrieved_at=retrieved_at,
            items=tuple(items),
            truncated=text_truncated or omitted,
        )
        if len(result.model_dump_json().encode("utf-8")) <= config.max_output_bytes:
            return result
        if not items:
            raise SearchError(SearchErrorCode.OUTPUT_TOO_LARGE)
        items.pop()
        omitted = True


class WebSearchReader:
    """Validate outbound text, call one injected provider, return bounded observations."""

    def __init__(
        self,
        provider: SearchProvider,
        config: SearchConfig,
        *,
        prohibited_patterns: Sequence[str] = (),
    ) -> None:
        self._provider = provider
        self._config = config
        self._prohibited_patterns = tuple(prohibited_patterns or ())

    async def _call_provider(self, request: SearchRequest, provider_id: str) -> SearchResult:
        response = await self._provider.search(
            request.query, min(request.max_results, self._config.max_results)
        )
        if not isinstance(response, ProviderSearchResponse):
            raise SearchError(SearchErrorCode.INVALID_PROVIDER_RESPONSE)
        return _normalize(response, request, self._config, provider_id)

    async def search(self, request: SearchRequest) -> SearchResult:
        if not isinstance(request, SearchRequest):
            raise SearchError(SearchErrorCode.INVALID_INPUT)
        if not self._config.external_search_enabled:
            raise SearchError(SearchErrorCode.EXTERNAL_SEARCH_DISABLED)
        # Same narrow check as Control: explicit credentials plus operator
        # prohibited patterns. The query never reaches the provider on block,
        # and the error carries a stable code only (no query text).
        verdict = check_outbound_text(request.query, self._prohibited_patterns)
        if verdict.blocked:
            raise SearchError(SearchErrorCode.SENSITIVE_QUERY)
        try:
            provider_id = self._provider.provider_id
            if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
                raise SearchError(SearchErrorCode.INVALID_PROVIDER_RESPONSE)
            return await asyncio.wait_for(
                self._call_provider(request, provider_id), timeout=self._config.timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            code = SearchErrorCode.TIMEOUT
        except ProviderFailure as exc:
            code = exc.code
        except SearchError:
            raise
        except ValidationError:
            code = SearchErrorCode.INVALID_PROVIDER_RESPONSE
        except Exception:
            code = SearchErrorCode.PROVIDER_ERROR
        # Raise outside the handler so the provider exception (which may include
        # the query or a credential) is not retained as __context__.
        raise SearchError(code)
