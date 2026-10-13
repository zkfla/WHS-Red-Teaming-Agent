"""Bounded network primitives, pinned to an explicitly authorized address."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.server.mcpserver.exceptions import ToolError

from redteam.capability.servers.config import ServerPolicy, TargetProfile
from redteam.capability.servers.session_state import cookie_header
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.redaction import SECRET_KEY_PARTS, redact_sensitive_text


class _PreviewHTML(HTMLParser):
    """Display HTML structure/text, excluding script and credential-bearing attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.hidden: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden = tag
            self.parts.append(f"<{tag}>[content omitted]")
            return
        attributes = []
        for key, value in attrs:
            if value is None:
                attributes.append(key)
                continue
            secret = any(part in key.lower().replace("-", "_") for part in SECRET_KEY_PARTS)
            if secret or key.startswith("on") or (tag in {"input", "meta"} and key in {
                "value", "content",
            }):
                value = "***REDACTED***"
            attributes.append(f'{key}="{escape(redact_sensitive_text(value), quote=True)}"')
        self.parts.append("<" + tag + (" " + " ".join(attributes) if attributes else "") + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in {"script", "style"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden == tag:
            self.hidden = None
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.hidden is None:
            self.parts.append(redact_sensitive_text(data))

    def handle_entityref(self, name):
        if self.hidden is None:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.hidden is None:
            self.parts.append(f"&#{name};")


class ToolInputError(ToolError, ValueError):
    """Messages contain only fixed explanations, never credentials or target responses.

    Also a ``ToolError`` (the MCP SDK's own "anticipated failure" type): the
    SDK treats any other exception as a crash and withholds its message from
    the client (``UnexpectedToolError`` -- see
    ``mcp.server.mcpserver.tools.base``), which used to leave an operator
    looking at an opaque "tool call failed" with no reason. A ``ToolError``
    is instead returned to the caller as ``is_error=True`` with this
    exception's own message intact -- exactly right for a deliberate,
    already-safe-to-share rejection like "target address outside configured
    networks", never a secret or a raw target response.
    """


@dataclass(frozen=True)
class ResolvedTarget:
    profile: TargetProfile
    host: str
    port: int
    addresses: tuple[str, ...]


async def resolve_target(policy: ServerPolicy, target_ref: str) -> ResolvedTarget:
    profile = policy.targets.get(target_ref)
    if profile is None:
        raise ToolInputError("unknown target_ref")
    parsed = urlsplit(profile.origin)
    host = parsed.hostname
    assert host is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        entries = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=policy.timeout_seconds,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ToolInputError("target DNS resolution failed") from exc
    addresses = tuple(sorted({str(entry[4][0]) for entry in entries}))
    networks = [ipaddress.ip_network(value) for value in policy.allowed_networks]
    if not addresses:
        raise ToolInputError("no target addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # Reject metadata/link-local, multicast and unspecified even if a broad CIDR is configured.
        if (
            ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or getattr(ip, "ipv4_mapped", None)
        ):
            raise ToolInputError("prohibited target address")
        if not any(ip in network for network in networks):
            raise ToolInputError("target address outside configured networks")
    return ResolvedTarget(profile, host, port, addresses)


def validate_path(profile: TargetProfile, path: str) -> None:
    if not profile.allows_path(path):
        raise ToolInputError("path outside configured allowlist")


def session_header(
    policy: ServerPolicy, target_ref: str, session_ref: str | None, path: str = "/"
) -> dict[str, str]:
    if session_ref is None:
        return {}
    session = policy.sessions.get(session_ref)
    if session is None or session.target_ref != target_ref:
        raise ToolInputError("session_ref is not bound to this target")
    if session.allowed_paths is not None and path not in session.allowed_paths:
        raise ToolInputError("path outside configured session allowlist")
    if session.storage_state_path:
        return {"Cookie": cookie_header(session, policy.targets[target_ref].origin, path)}
    assert session.bearer_env is not None
    token = os.environ.get(session.bearer_env)
    if not token or any(c in token for c in "\r\n\x00"):
        raise ToolInputError("session credential unavailable")
    return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class CapturedResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    # Preserve individual fields: joining Set-Cookie breaks Expires and cookie boundaries.
    set_cookie_headers: tuple[str, ...] = field(default=(), repr=False)
    # Populated by the live request path when the target metadata is known.
    # Optional keeps fixture responses honest: absent values are not guessed.
    host: str | None = None
    port: int | None = None
    scheme: str | None = None
    protocol: str | None = None
    url: str | None = None
    # Opaque receipt of which configured session was actually used for this
    # live request: str means that named session, None means anonymous.
    # Only the live request path sets has_session_receipt; fixture/HAR
    # captures leave it False so an absent receipt is unknown, never proof
    # of anonymous use. Never a token/cookie value, only the opaque ref.
    actual_session_ref: str | None = None
    has_session_receipt: bool = False

    def summary(self, *, body_max_chars: int = 0) -> dict[str, Any]:
        # No raw response, header values, URL query or token is returned to the model.
        summary = {
            "status_code": self.status,
            "status": self.status,
            "body_bytes": len(self.body),
            "body_sha256": hashlib.sha256(self.body).hexdigest(),
            "redirect": 300 <= self.status < 400,
            "security_headers_present": {
                key: key in self.headers
                for key in (
                    "strict-transport-security",
                    "content-security-policy",
                    "x-content-type-options",
                    "cache-control",
                )
            },
            "body_omitted": True,
        }
        for key, value in (
            ("host", self.host),
            ("port", self.port),
            ("scheme", self.scheme),
            ("protocol", self.protocol),
            ("service", "http" if self.scheme in {"http", "https"} else None),
            ("url", self.url),
        ):
            if value is not None:
                summary[key] = value
        if self.has_session_receipt:
            # Per-request actual-use receipt. None here means this request
            # actually ran anonymous; absent means unknown (fixture/capture).
            summary["actual_session_ref"] = self.actual_session_ref
        if body_max_chars:
            content_type = self.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type.startswith("text/") or content_type.endswith(("/json", "+json")):
                text = self.body.decode("utf-8", errors="replace")
                if content_type == "text/html":
                    parser = _PreviewHTML()
                    parser.feed(text)
                    text = "".join(parser.parts)
                    summary["body_preview_transform"] = (
                        "html_scripts_and_sensitive_attributes_omitted"
                    )
                try:
                    # Redact the complete JSON before clipping; a cut token must not leak.
                    text = json.dumps(redact_secret_material(json.loads(text)), ensure_ascii=False)
                except (ValueError, RecursionError):
                    if content_type.endswith(("/json", "+json")):
                        summary["body_preview_unavailable"] = "invalid_or_too_deep_json"
                        return summary
                    text = redact_sensitive_text(text)
                text = redact_sensitive_text(text)
                summary.update(body_preview=text[:body_max_chars], body_omitted=False,
                               body_preview_truncated=len(text) > body_max_chars,
                               body_preview_is_untrusted_data=True)
        return summary


async def request(
    policy: ServerPolicy,
    target_ref: str,
    path: str,
    *,
    method: str = "GET",
    session_ref: str | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    request_headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CapturedResponse:
    profile = policy.targets.get(target_ref)
    if profile is None:
        raise ToolInputError("unknown target_ref")
    validate_path(profile, path)
    allowed_methods = profile.allowed_path_methods.get(path, profile.allowed_methods)
    if method not in allowed_methods:
        raise ToolInputError("HTTP method is not enabled for this target")
    if json_body is not None and form_body is not None:
        raise ToolInputError("select one request body format")
    if (json_body is not None or form_body is not None) and (
        method in {"GET", "HEAD", "OPTIONS"} or not profile.request_body_enabled
    ):
        raise ToolInputError("body is not allowed for read requests")
    content = None if json_body is None else json.dumps(json_body).encode()
    if form_body is not None:
        from urllib.parse import urlencode
        content = urlencode(form_body).encode()
    if content is not None and len(content) > 65536:
        raise ToolInputError("request body exceeds limit")
    query = query or {}
    request_headers = request_headers or {}
    if len(query) > 20 or any(
        not profile.allows_query_key(path, key)
        or len(value) > 1024 or any(ord(c) < 32 for c in value)
        for key, value in query.items()
    ):
        raise ToolInputError("query outside configured allowlist or size limit")
    headers = {key.lower(): value for key, value in request_headers.items()}
    if len(headers) != len(request_headers) or len(headers) > 20 or any(
        not profile.allows_request_header(key) or len(value) > 1024
        or any(ord(c) < 32 or ord(c) > 126 for c in value)
        for key, value in headers.items()
    ):
        raise ToolInputError("request headers outside configured allowlist or size limit")
    headers.update(session_header(policy, target_ref, session_ref, path))
    target = await resolve_target(policy, target_ref)
    host = f"[{target.host}]" if ":" in target.host else target.host
    headers.update({"Host": f"{host}:{target.port}", "Accept-Encoding": "identity"})
    if content is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form_body is not None else "application/json"
        )
    # Connect to the validated IP, preserving HTTP Host and TLS hostname verification/SNI.
    url = httpx.URL(profile.origin + path).copy_with(host=target.addresses[0])
    try:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=policy.timeout_seconds,
        ) as client:
            with anyio.fail_after(policy.timeout_seconds):
                async with client.stream(
                    method,
                    url,
                    headers=headers,
                    content=content,
                    params=query,
                    extensions={"sni_hostname": target.host},
                ) as response:
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ToolInputError("compressed response is not accepted")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > policy.max_response_bytes:
                            raise ToolInputError("response exceeds configured byte limit")
                    return CapturedResponse(
                        response.status_code,
                        dict(response.headers),
                        bytes(body),
                        tuple(response.headers.get_list("set-cookie")),
                        host=target.host,
                        port=target.port,
                        scheme=url.scheme,
                        protocol=response.http_version,
                        url=profile.origin + path,
                        actual_session_ref=session_ref,
                        has_session_receipt=True,
                    )
    except (httpx.HTTPError, OSError, asyncio.TimeoutError, TimeoutError) as exc:
        raise ToolInputError("target HTTP request failed") from exc
