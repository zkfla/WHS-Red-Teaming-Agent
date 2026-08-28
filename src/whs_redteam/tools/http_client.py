"""Scope- and budget-controlled HTTP client for safe deterministic checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urljoin

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.sanitizer import sanitize_data, sanitize_url
from whs_redteam.tools.network_gate import NetworkGate


@dataclass(frozen=True, slots=True)
class SafeHttpResponse:
    url: str
    status_code: int
    headers: httpx.Headers
    body: bytes
    elapsed_ms: float
    request_fingerprint: str = ""
    response_fingerprint: str = ""

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class SafeHttpClient:
    def __init__(
        self,
        network_gate: NetworkGate,
        audit_logger: AuditLogger,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10,
        max_body_bytes: int = 262_144,
        max_redirects: int = 5,
        default_headers: Mapping[str, str] | None = None,
        initial_cookies: Mapping[str, str] | None = None,
    ) -> None:
        self.network_gate = network_gate
        self.audit_logger = audit_logger
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.max_redirects = max_redirects
        self._default_headers = dict(default_headers or {})
        # This jar belongs to one client. ActorSessionPool creates a separate
        # client per actor, preventing cookies from crossing identity boundaries.
        self._cookies = httpx.Cookies(initial_cookies)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> SafeHttpResponse:
        return await self.request("GET", url, headers=headers)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> SafeHttpResponse:
        current_url = url
        request_headers = {**self._default_headers, **(headers or {})}
        method = method.upper()
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout_seconds,
            follow_redirects=False,
            cookies=self._cookies,
        ) as client:
            for redirect_count in range(self.max_redirects + 1):
                await self.network_gate.authorize(current_url, method)
                started = monotonic()
                async with client.stream(method, current_url, headers=request_headers) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        remaining = self.max_body_bytes - len(body)
                        if remaining <= 0:
                            break
                        body.extend(chunk[:remaining])
                    elapsed_ms = (monotonic() - started) * 1000
                    self._cookies.update(client.cookies)
                    location = response.headers.get("location")
                    if response.is_redirect and location:
                        if redirect_count >= self.max_redirects:
                            raise httpx.TooManyRedirects("maximum redirect count exceeded")
                        next_url = urljoin(str(response.url), location)
                        self.audit_logger.emit(
                            "http_redirect_observed",
                            source_url=sanitize_url(str(response.url)),
                            target_url=sanitize_url(next_url),
                            status_code=response.status_code,
                        )
                        current_url = next_url
                        continue
                    snapshot = SafeHttpResponse(
                        url=str(response.url),
                        status_code=response.status_code,
                        headers=response.headers,
                        body=bytes(body),
                        elapsed_ms=elapsed_ms,
                        request_fingerprint=self._request_fingerprint(
                            method, str(response.request.url), request_headers
                        ),
                        response_fingerprint=self._response_fingerprint(
                            response.status_code,
                            response.headers,
                            bytes(body),
                        ),
                    )
                    self.audit_logger.emit(
                        "http_response_observed",
                        url=sanitize_url(snapshot.url),
                        status_code=snapshot.status_code,
                        body_bytes=len(snapshot.body),
                        elapsed_ms=round(snapshot.elapsed_ms, 2),
                    )
                    return snapshot
        raise httpx.TooManyRedirects("maximum redirect count exceeded")

    @staticmethod
    def _request_fingerprint(method: str, url: str, headers: dict[str, str]) -> str:
        material = {
            "method": method,
            "url": sanitize_url(url),
            "headers": sanitize_data(headers),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _response_fingerprint(
        status_code: int,
        headers: httpx.Headers,
        body: bytes,
    ) -> str:
        material = {
            "status_code": status_code,
            "headers": sanitize_data(dict(headers)),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()
