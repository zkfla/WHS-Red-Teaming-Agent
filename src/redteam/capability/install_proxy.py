"""Separate install-route proxy config. Owned by Capability (TASK-005).

The install route is a disjoint proxy path: installer processes spawned by
Control get proxy env pointing at ONE explicit endpoint whose upstream is
bound to the allowlist-pinned mirror IPs. The target chain can never reach
mirror IPs and the install path can never reach target IPs (disjoint nft IP
sets, checked at policy load). User programs are never registered as new
function tools; standard ``pip``/``npm``/``apt`` run through proxy env only.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from redteam.capability.gated_network_policy import InstallProxyConfig
from redteam.capability.install_proxy_engine import (
    PinningForwardProxy,
    ProxyRefused,
)


@dataclass(frozen=True)
class InstallProxyRoute:
    """One install path: client transport plus mirror authority, both explicit."""

    endpoint: str
    proxy_port: int
    upstream_host: str
    upstream_port: int
    pinned_mirror_ips: tuple[str, ...]


def endpoint_port(endpoint: str, *, what: str) -> int:
    """Explicit endpoint port, else the known scheme port; never guessed."""
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        raise ValueError(f"{what} endpoint is not a valid URL") from None
    try:
        explicit = parts.port
    except ValueError:
        raise ValueError(f"{what} endpoint carries an invalid port") from None
    if explicit is not None:
        return explicit
    if parts.scheme == "http":
        return 80
    if parts.scheme == "https":
        return 443
    raise ValueError(f"{what} endpoint needs an explicit or known-scheme port")


def _origin_host(endpoint: str, *, what: str) -> str:
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        raise ValueError(f"{what} endpoint is not a valid URL") from None
    if not host:
        raise ValueError(f"{what} endpoint has no host")
    return host


def route_for(
    config: InstallProxyConfig | None,
    *,
    mirror_name: str,
    mirror_ips: tuple[str, ...],
) -> InstallProxyRoute:
    """Build the install route with explicit transport and authority ports."""
    if config is None:
        raise ValueError("install route unconfigured")
    if not mirror_name.strip():
        raise ValueError("install route needs a mirror name")
    if not mirror_ips:
        raise ValueError("install route needs pinned mirror IPs")
    return InstallProxyRoute(
        endpoint=config.endpoint,
        proxy_port=endpoint_port(config.endpoint, what="proxy"),
        upstream_host=_origin_host(config.mirror_upstream, what="mirror upstream"),
        upstream_port=endpoint_port(config.mirror_upstream, what="mirror upstream"),
        pinned_mirror_ips=tuple(mirror_ips),
    )


def proxy_env(route: InstallProxyRoute) -> dict[str, str]:
    """Forward-proxy env for provider-seeded installer processes.

    A forward proxy is not a package index: this carries no PIP_INDEX_URL
    and no registry override. The package index comes from the
    provider-owned install recipe; these variables only steer HTTP(S)
    through the proxy, whose upstream dials pinned mirror IPs with no DNS.
    Model-supplied proxy or index variables must never override these.
    """
    return {
        "HTTP_PROXY": route.endpoint,
        "HTTPS_PROXY": route.endpoint,
        "http_proxy": route.endpoint,
        "https_proxy": route.endpoint,
        "NO_PROXY": "127.0.0.1,localhost",
    }


def engine_for_route(
    route: InstallProxyRoute,
    *,
    names: tuple[str, ...] = (),
    allow_ports: tuple[int, ...] = (80, 443, 8080),
) -> PinningForwardProxy:
    """Bind the forward-proxy upstream to exactly the route mirror IPs.

    The engine dials only these canonical IPv4 literals and never
    resolves DNS, so even a caller-chosen upstream outside the pins is
    refused at dial. Non-canonical or non-literal pins are refused here,
    matching the registry contract.
    Extra hostname keys (the mirror name and upstream host) map to the
    same pins so recipe index URLs using names resolve without DNS.
    """
    pins: dict[str, tuple[str, ...]] = {}
    for ip in route.pinned_mirror_ips:
        try:
            canonical = str(ipaddress.ip_address(ip))
        except ValueError:
            raise ProxyRefused("non-literal mirror pin") from None
        if canonical != ip or ":" in canonical:
            raise ProxyRefused("non-canonical mirror pin")
        pins.setdefault(canonical, (canonical,))
    if not pins:
        raise ProxyRefused("install route needs pinned mirror IPs")
    dial_ips = tuple(pins)
    for name in names:
        key = name.strip().lower().rstrip(".")
        if key:
            pins.setdefault(key, dial_ips)
    return PinningForwardProxy(pins, allow_ports=allow_ports)


def nft_ipset_elements(ips: tuple[str, ...]) -> tuple[str, ...]:
    """Sorted unique IP literals for an nft set element list."""
    return tuple(sorted(set(ips)))
