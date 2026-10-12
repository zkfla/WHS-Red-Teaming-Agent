"""Loopback-only access rules for the web surface (plan3 W5).

No access token is used. Instead every request must come from an allowed peer
address, carry an allowed ``Host`` and, when it writes, prove it was issued by
the served page (exact ``Origin``, JSON body, custom header). A different local
program that calls the API directly is not stopped by these rules.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

CLIENT_HEADER = "x-redteam-client"
CLIENT_HEADER_VALUE = "web"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def validate_bind_host(host: str) -> str:
    """Return a loopback IP literal, or raise ``ValueError`` with a stable code."""
    candidate = "127.0.0.1" if host.strip().lower() == "localhost" else host.strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        raise ValueError("bind_address_invalid") from None
    if address.is_unspecified or not address.is_loopback:
        raise ValueError("bind_address_must_be_loopback")
    return str(address)


def normalize_origin(value: str) -> str:
    """Exact origin form ``scheme://host[:port]``; wildcards and paths are refused."""
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or "*" in value
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ValueError("origin_invalid")
    return f"{parts.scheme}://{parts.netloc.lower()}"


@dataclass(frozen=True)
class GuardPolicy:
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    peer_networks: tuple = _LOOPBACK_NETWORKS
    #: Literal peer strings accepted in addition to the networks (test clients).
    extra_peers: frozenset[str] = frozenset()

    @classmethod
    def for_loopback(cls, port: int, *, extra_origins: tuple[str, ...] = ()) -> GuardPolicy:
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        origins = {f"http://{host}" for host in hosts}
        origins.update(normalize_origin(origin) for origin in extra_origins)
        return cls(frozenset(hosts), frozenset(origins))

    def peer_allowed(self, peer: str | None) -> bool:
        if peer is None:
            return False
        if peer in self.extra_peers:
            return True
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        return any(address in network for network in self.peer_networks)


class GuardMiddleware:
    """Pure ASGI guard: rejects before any route or static file is reached."""

    def __init__(self, app, policy: GuardPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # no websocket surface exists
        denied = self._denied(scope)
        if denied is not None:
            response = JSONResponse({"error": denied}, status_code=403)
            await response(scope, receive, self._with_headers(scope, send))
            return
        await self.app(scope, receive, self._with_headers(scope, send))

    def _with_headers(self, scope, send):
        api = scope.get("path", "").startswith("/api/")

        async def wrapped(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"no-referrer"))
                headers.append((b"content-security-policy", _CSP.encode()))
                if api:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    def _denied(self, scope) -> str | None:
        client = scope.get("client")
        if not self.policy.peer_allowed(client[0] if client else None):
            return "peer_not_allowed"
        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers", [])}
        if headers.get("host", "").lower() not in self.policy.allowed_hosts:
            return "host_not_allowed"
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.policy.allowed_origins:
            return "origin_not_allowed"
        if scope["method"] in WRITE_METHODS:
            if origin is None:
                return "origin_required"
            if headers.get(CLIENT_HEADER) != CLIENT_HEADER_VALUE:
                return "client_header_required"
            if not headers.get("content-type", "").lower().startswith("application/json"):
                return "json_required"
        return None
