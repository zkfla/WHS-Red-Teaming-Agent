"""Bounded network primitives, pinned to an explicitly authorized address."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from redteam.capability.servers.config import ServerPolicy, TargetProfile


class ToolInputError(ValueError):
    """Messages contain only fixed explanations, never credentials or target responses."""


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
    # An exact path list avoids URL joining ambiguities and accidental traversal of financial flows.
    if path not in profile.allowed_paths:
        raise ToolInputError("path outside configured allowlist")


def session_header(
    policy: ServerPolicy, target_ref: str, session_ref: str | None
) -> dict[str, str]:
    if session_ref is None:
        return {}
    session = policy.sessions.get(session_ref)
    if session is None or session.target_ref != target_ref:
        raise ToolInputError("session_ref is not bound to this target")
    token = os.environ.get(session.bearer_env)
    if not token or any(c in token for c in "\r\n\x00"):
        raise ToolInputError("session credential unavailable")
    return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class CapturedResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def summary(self) -> dict[str, Any]:
        # No raw response, header values, URL query or token is returned to the model.
        return {
            "status_code": self.status,
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


async def request(
    policy: ServerPolicy,
    target_ref: str,
    path: str,
    *,
    method: str = "GET",
    session_ref: str | None = None,
    json_body: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CapturedResponse:
    profile = policy.targets.get(target_ref)
    if profile is None:
        raise ToolInputError("unknown target_ref")
    validate_path(profile, path)
    if method not in profile.allowed_methods:
        raise ToolInputError("HTTP method is not enabled for this target")
    if json_body is not None and method in {"GET", "HEAD", "OPTIONS"}:
        raise ToolInputError("body is not allowed for read requests")
    content = None if json_body is None else json.dumps(json_body).encode()
    if content is not None and len(content) > 65536:
        raise ToolInputError("request body exceeds limit")
    headers = session_header(policy, target_ref, session_ref)
    target = await resolve_target(policy, target_ref)
    host = f"[{target.host}]" if ":" in target.host else target.host
    headers.update({"Host": f"{host}:{target.port}", "Accept-Encoding": "identity"})
    if content is not None:
        headers["Content-Type"] = "application/json"
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
                        response.status_code, dict(response.headers), bytes(body)
                    )
    except (httpx.HTTPError, OSError, asyncio.TimeoutError, TimeoutError) as exc:
        raise ToolInputError("target HTTP request failed") from exc
