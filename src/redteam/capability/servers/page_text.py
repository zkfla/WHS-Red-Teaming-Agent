"""Internal, operator-scoped public page reader; no MCP or Agent registration."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from email.message import Message
from enum import Enum
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from redteam.core.outbound_text import check_outbound_text


class PageTextErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    ACCESS_DENIED = "access_denied"
    DNS_ERROR = "dns_error"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    REDIRECT_ERROR = "redirect_error"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    TOO_LARGE = "too_large"


class PageTextError(ValueError):
    """Stable error code; never attach raw URL, response, or upstream exception."""

    def __init__(self, code: PageTextErrorCode) -> None:
        self.code = code
        super().__init__(f"page_text: {code.value}")


class _LocalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def _url(value: str) -> httpx.URL:
    # Reject ambiguity before either urllib or HTTPX can silently normalize it.
    if (
        len(value) > 8192
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or "\\" in value
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise PageTextError(PageTextErrorCode.INVALID_INPUT)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "%" in parsed.netloc
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        result = httpx.URL(value)
        host = result.raw_host.decode("ascii").lower().rstrip(".")
        if not host or ".." in host:
            raise ValueError
        if ":" not in host and not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host
        ):
            raise ValueError
        return result.copy_with(
            host=host, port=port if port != 443 else None, raw_path=result.raw_path
        )
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise PageTextError(PageTextErrorCode.INVALID_INPUT) from None


def _origin(url: httpx.URL) -> tuple[str, int]:
    return url.raw_host.decode("ascii"), url.port or 443


class PageTextConfig(_LocalModel):
    """Exact HTTPS origins. Empty tuple denies everything; no wildcard/subdomains."""

    allowed_origins: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=5.0, gt=0, le=30, allow_inf_nan=False, strict=True)
    max_download_bytes: int = Field(default=262144, ge=1, le=1048576, strict=True)
    max_return_chars: int = Field(default=20000, ge=1, le=262144, strict=True)
    # Compat input only: one hop is returned per call and never auto-followed.
    # max_redirects no longer enables chained fetching; it stays validated.
    max_redirects: int = Field(default=3, ge=0, le=5, strict=True)

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        origins = []
        for value in values:
            try:
                url = _url(value)
                if url.path != "/" or url.query or "?" in value or "#" in value:
                    raise ValueError
            except ValueError:
                raise ValueError("allowed_origins must contain HTTPS origins only") from None
            origins.append(str(url).rstrip("/"))
        return tuple(sorted(set(origins)))


class PageTextRequest(_LocalModel):
    url: str = Field(min_length=1, max_length=8192, strict=True, repr=False)
    max_chars: int | None = Field(default=None, ge=1, strict=True)


class PageTextResult(_LocalModel):
    requested_url: str = Field(repr=False)
    final_url: str = Field(repr=False)
    status_code: int
    content_type: str
    title: str | None = Field(default=None, repr=False)
    text: str = Field(repr=False)
    truncated: bool
    redirect_url: str | None = Field(default=None, repr=False)
    body_present: bool = True


Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
TransportFactory = Callable[[], httpx.AsyncBaseTransport]


async def _resolve(host: str, port: int) -> tuple[str, ...]:
    entries = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return tuple(sorted({str(entry[4][0]) for entry in entries}))


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise PageTextError(PageTextErrorCode.DNS_ERROR) from None
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or "%" in value
        # Conservative exclusions across the project's Python 3.10+ versions;
        # ipaddress special-purpose classifications differ between releases.
        or (
            isinstance(address, ipaddress.IPv4Address)
            and (
                address in ipaddress.IPv4Network("192.0.0.0/24")
                or address in ipaddress.IPv4Network("192.88.99.0/24")
            )
        )
        or (
            isinstance(address, ipaddress.IPv6Address)
            and (
                address not in ipaddress.IPv6Network("2000::/3")
                or address in ipaddress.IPv6Network("2001::/23")
                or address in ipaddress.IPv6Network("3fff::/20")
                or address.ipv4_mapped is not None
                or address.sixtofour is not None
                or address.teredo is not None
            )
        )
    ):
        raise PageTextError(PageTextErrorCode.ACCESS_DENIED)
    return str(address)


def _transport() -> httpx.AsyncHTTPTransport:
    # HTTPcore DEBUG traces include response headers (cookies/Location). Do not
    # change process-wide logging or silently expose them in this restricted path.
    if any(logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in (
        "httpcore.connection", "httpcore.http11", "httpcore.http2",
    )):
        raise PageTextError(PageTextErrorCode.CONNECTION_ERROR)
    return httpx.AsyncHTTPTransport(
        verify=True, trust_env=False, retries=0, http2=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )


class PageTextReader:
    """Trusted construction-time DI only; request data cannot change policy/I/O."""

    def __init__(
        self,
        config: PageTextConfig,
        *,
        resolver: Resolver = _resolve,
        transport_factory: TransportFactory = _transport,
        prohibited_patterns: Sequence[str] = (),
    ) -> None:
        self._config = config
        self._allowed = frozenset(_origin(_url(value)) for value in config.allowed_origins)
        self._resolver = resolver
        self._transport_factory = transport_factory
        self._prohibited_patterns = tuple(prohibited_patterns or ())

    async def fetch_page_text(self, request: PageTextRequest) -> PageTextResult:
        limit = request.max_chars or self._config.max_return_chars
        if limit > self._config.max_return_chars:
            raise PageTextError(PageTextErrorCode.INVALID_INPUT)
        verdict = check_outbound_text(request.url, self._prohibited_patterns)
        if verdict.blocked:
            raise PageTextError(PageTextErrorCode.INVALID_INPUT)
        started = asyncio.get_running_loop().time()
        try:
            result = await asyncio.wait_for(
                self._fetch(request.url, limit), timeout=self._config.timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            raise PageTextError(PageTextErrorCode.TIMEOUT) from None
        except (httpx.HTTPError, OSError):
            raise PageTextError(PageTextErrorCode.CONNECTION_ERROR) from None
        if asyncio.get_running_loop().time() - started > self._config.timeout_seconds:
            raise PageTextError(PageTextErrorCode.TIMEOUT)
        return result

    async def _address(self, url: httpx.URL) -> str:
        host, port = _origin(url)
        if (host, port) not in self._allowed:
            raise PageTextError(PageTextErrorCode.ACCESS_DENIED)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return _public_ip(str(literal))
        try:
            addresses = await self._resolver(host, port)
        except (asyncio.TimeoutError, TimeoutError):
            raise PageTextError(PageTextErrorCode.TIMEOUT) from None
        except (OSError, ValueError):
            raise PageTextError(PageTextErrorCode.DNS_ERROR) from None
        if not addresses:
            raise PageTextError(PageTextErrorCode.DNS_ERROR)
        # Reject mixed public/private answers as well as wholly private answers.
        checked = tuple(_public_ip(address) for address in addresses)
        return checked[0]

    async def _fetch(self, requested_url: str, limit: int) -> PageTextResult:
        # One hop only: a single request per call, never auto-followed.
        # A redirect response is returned as data (redirect_url set,
        # body_present False); the target is never fetched here.
        url = _url(requested_url)
        address = await self._address(url)
        host, port = _origin(url)
        authority = f"[{host}]" if ":" in host else host
        if port != 443:
            authority += f":{port}"
        # Fresh direct transport for one hop: no proxy, client cookie jar,
        # authentication, HTTPX URL logging, redirect handling or retries.
        async with self._transport_factory() as transport:
            wire_request = httpx.Request(
                "GET", url.copy_with(host=address),
                headers={"Host": authority, "Accept-Encoding": "identity",
                         "Accept": "text/html, text/plain"},
                extensions={
                    "sni_hostname": host,
                    "timeout": dict.fromkeys(
                        ("connect", "read", "write", "pool"), self._config.timeout_seconds
                    ),
                },
            )
            response = await transport.handle_async_request(wire_request)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = _redirect(url, response)
                    return PageTextResult(
                        requested_url=requested_url, final_url=str(url),
                        status_code=response.status_code, content_type="",
                        title=None, text="", truncated=False,
                        redirect_url=str(target), body_present=False,
                    )
                if not 200 <= response.status_code < 300:
                    raise PageTextError(PageTextErrorCode.HTTP_ERROR)
                mime, encoding = _format(response)
                body = await _body(response, self._config.max_download_bytes)
                title, text = extract_page_text(body, mime, encoding)
                return PageTextResult(
                    requested_url=requested_url, final_url=str(url),
                    status_code=response.status_code, content_type=mime,
                    title=title[:limit] if title else None, text=text[:limit],
                    truncated=len(text) > limit or bool(title and len(title) > limit),
                    redirect_url=None, body_present=True,
                )
            finally:
                await response.aclose()


def _redirect(url: httpx.URL, response: httpx.Response) -> httpx.URL:
    locations = response.headers.get_list("location")
    if len(locations) != 1 or not locations[0]:
        raise PageTextError(PageTextErrorCode.REDIRECT_ERROR)
    location = locations[0]
    if any(ord(char) <= 32 or ord(char) == 127 for char in location) or "\\" in location:
        raise PageTextError(PageTextErrorCode.REDIRECT_ERROR)
    try:
        return _url(urljoin(str(url), location))
    except ValueError:
        raise PageTextError(PageTextErrorCode.REDIRECT_ERROR) from None


def _format(response: httpx.Response) -> tuple[str, str]:
    if response.headers.get("content-encoding", "identity").lower().strip() != "identity":
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_ENCODING)
    types = response.headers.get_list("content-type")
    if len(types) != 1:
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_FORMAT)
    header = Message()
    header["content-type"] = types[0]
    mime = types[0].partition(";")[0].strip().lower()
    if mime not in {"text/html", "text/plain"}:
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_FORMAT)
    parameters = header.get_params() or []
    charsets = [value for key, value in parameters[1:] if key.lower() == "charset"]
    if len(charsets) > 1 or (charsets and not isinstance(charsets[0], str)):
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_ENCODING)
    try:
        encoding = codecs.lookup(charsets[0] if charsets else "utf-8").name
    except (LookupError, ValueError):
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_ENCODING) from None
    if encoding not in {"utf-8", "ascii", "iso8859-1", "cp1252", "euc_kr", "cp949"}:
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_ENCODING)
    return mime, encoding


async def _body(response: httpx.Response, maximum: int) -> bytes:
    length = response.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 20:
            raise PageTextError(PageTextErrorCode.HTTP_ERROR)
        if int(length) > maximum:
            raise PageTextError(PageTextErrorCode.TOO_LARGE)
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(chunk) > maximum - len(body):
            raise PageTextError(PageTextErrorCode.TOO_LARGE)
        body.extend(chunk)
    return bytes(body)


class _HTMLText(HTMLParser):
    _HIDDEN = frozenset({"script", "style", "template", "noscript", "iframe", "object", "svg"})
    _BREAKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "hr",
                         "section", "article", "header", "footer", "blockquote", "pre", "td"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.hidden: list[str] = []
        self.in_head = False
        self.in_title = False
        self.title_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden:
            if tag in self._HIDDEN:
                self.hidden.append(tag)
            return
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        elif tag == "head":
            self.in_head = True
        elif tag == "title" and not self.title_seen:
            self.in_title = True
            self.title_seen = True
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "head":
            self.in_head = False
        elif tag == "title":
            self.in_title = False
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.in_title:
            self.title_parts.append(data)
        elif not self.in_head:
            self.parts.append(data)


def extract_page_text(body: bytes, mime: str, encoding: str) -> tuple[str | None, str]:
    """Pure extraction; caller bounds body first. No DOM/CSS/JS or external loads."""
    try:
        decoded = body.decode(encoding, errors="strict")
    except (UnicodeError, LookupError):
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_ENCODING) from None
    decoded = decoded.removeprefix("\ufeff")
    if mime == "text/plain":
        return None, decoded
    if mime != "text/html":
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_FORMAT)
    parser = _HTMLText()
    try:
        parser.feed(decoded)
        parser.close()
    except (ValueError, AssertionError):
        raise PageTextError(PageTextErrorCode.UNSUPPORTED_FORMAT) from None
    title = " ".join("".join(parser.title_parts).split()) or None
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return title, "\n".join(line for line in lines if line)
