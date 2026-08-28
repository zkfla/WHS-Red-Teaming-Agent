"""Resolve approved targets once and pin Chromium to validated addresses."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import DnsPolicyViolation

Resolver = Callable[[str], Iterable[str]]
METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def system_resolver(hostname: str) -> tuple[str, ...]:
    records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return tuple(sorted({record[4][0] for record in records}))


def _validate_address(
    hostname: str,
    address: str,
    *,
    allow_private_dns: bool,
    literal_target: bool,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(address)
    if (
        ip in METADATA_ADDRESSES
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise DnsPolicyViolation(f"target {hostname} resolves to forbidden address class")
    if (ip.is_private or ip.is_loopback) and not (
        literal_target or hostname == "localhost" or allow_private_dns
    ):
        raise DnsPolicyViolation(
            f"target {hostname} resolves to a private address without allow_private_dns"
        )
    return ip


@dataclass(frozen=True, slots=True)
class DnsPinSet:
    addresses: dict[str, tuple[str, ...]]

    def chromium_args(self) -> list[str]:
        mappings: list[str] = []
        for hostname, addresses in sorted(self.addresses.items()):
            try:
                ipaddress.ip_address(hostname)
                continue
            except ValueError:
                pass
            chosen = sorted(addresses, key=lambda item: (":" in item, item))[0]
            mappings.append(f"MAP {hostname} {chosen}")
        if not mappings:
            return []
        return [f"--host-resolver-rules={','.join(mappings)}"]


def resolve_and_pin_targets(
    config: EngagementConfig,
    resolver: Resolver = system_resolver,
) -> DnsPinSet:
    pinned: dict[str, tuple[str, ...]] = {}
    for target in config.targets:
        hostname = urlsplit(target).hostname
        if hostname is None:
            raise DnsPolicyViolation("target has no hostname")
        try:
            ipaddress.ip_address(hostname)
            addresses = (hostname,)
            literal = True
        except ValueError:
            try:
                addresses = tuple(resolver(hostname))
            except OSError as exc:
                raise DnsPolicyViolation(f"cannot resolve approved target {hostname}") from exc
            literal = False
        if not addresses:
            raise DnsPolicyViolation(f"approved target {hostname} has no addresses")
        validated = tuple(
            str(
                _validate_address(
                    hostname,
                    address,
                    allow_private_dns=config.allow_private_dns,
                    literal_target=literal,
                )
            )
            for address in addresses
        )
        pinned[hostname.lower().rstrip(".")] = validated
    return DnsPinSet(pinned)
