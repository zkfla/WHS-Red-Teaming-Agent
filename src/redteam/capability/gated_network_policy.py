"""Operator-owned gated-network config. Owned by Capability (TASK-005).

There is deliberately no network knob on ``ShellWorkroomPolicy``: when this
policy object is absent (``None`` at the gated-window extension), every gated
path is refused and the existing closed local-script path works unchanged.
All budget numbers are explicit operator config; there is no unlimited
default anywhere in this module.

Contact root BEFORE changing any ``core.contracts.network`` type field;
this module only *consumes* those types.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redteam.core.contracts.network import NetworkBudget, NetworkPurpose, WindowCaps

# External IPv6 egress is UNSUPPORTED and fail-closed: the ruleset builder
# (``gated_window``) never emits an accept element for a non-loopback IPv6
# destination, and there is intentionally no flag to flip. Loopback proofs
# are never presented as external support. Lifting this needs a real
# allowed-external-IPv6 proof first.
EXTERNAL_IPV6 = "unsupported-fail-closed"


class PinnedRegistry(BaseModel):
    """Out-of-band name-to-IP pins. Resolved by the operator, never by model traffic.

    ``targets`` maps a scope registry name to its pinned destination IPs;
    ``mirrors`` maps a package-mirror name to its pinned IPs. Scope text
    keeps the human registry name; enforcement uses only the pinned IPs.
    No TTL refresh ever happens from model traffic: pins change only when
    the operator reloads this config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    mirrors: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("targets", "mirrors", mode="before")
    @classmethod
    def _tuples(cls, value: object) -> object:
        if isinstance(value, dict):
            return {str(k): tuple(str(i) for i in v) for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _literals_and_disjoint(self) -> PinnedRegistry:
        seen: dict[str, str] = {}
        for section in (self.targets, self.mirrors):
            for name, ips in section.items():
                if not name or len(name) > 128:
                    raise ValueError("registry names must be 1..128 chars")
                if not ips:
                    raise ValueError(f"registry entry {name!r} pins no IPs")
                for ip in ips:
                    try:
                        canonical = str(ipaddress.ip_address(ip))
                    except ValueError:
                        raise ValueError(
                            f"registry entry {name!r} pins non-literal {ip!r}"
                        ) from None
                    if canonical != ip:
                        raise ValueError(
                            f"registry entry {name!r} pins non-canonical IP {ip!r}"
                        )
                    owner = seen.setdefault(canonical, name)
                    if owner != name:
                        raise ValueError(f"pinned IP {canonical!r} in two entries")
        target_ips = {str(ipaddress.ip_address(ip)) for ips in self.targets.values() for ip in ips}
        mirror_ips = {str(ipaddress.ip_address(ip)) for ips in self.mirrors.values() for ip in ips}
        if target_ips & mirror_ips:
            raise ValueError("target and mirror pinned IPs must be disjoint")
        return self

    def target_ips(self) -> tuple[str, ...]:
        """Sorted pinned target IPs (v4+v6 literals as configured)."""
        return tuple(sorted({ip for ips in self.targets.values() for ip in ips}))

    def mirror_ips(self) -> tuple[str, ...]:
        """Sorted pinned mirror IPs."""
        return tuple(sorted({ip for ips in self.mirrors.values() for ip in ips}))


class InstallProxyConfig(BaseModel):
    """The separate install route: one explicit proxy endpoint, pinned hosts only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str = Field(min_length=1, max_length=256)
    pinned_hosts: tuple[str, ...] = Field(min_length=1)
    mirror_upstream: str = Field(min_length=1, max_length=256)

    @field_validator("endpoint")
    @classmethod
    def _http_endpoint(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("install proxy endpoint must be http(s)")
        return value

    @field_validator("mirror_upstream")
    @classmethod
    def _upstream_origin(cls, value: str) -> str:
        """Explicit upstream origin; the port is authority, never guessed."""
        try:
            parts = urlsplit(value)
        except ValueError:
            raise ValueError("mirror upstream must be an http(s) origin") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("mirror upstream must be an http(s) origin with a host")
        try:
            _port = parts.port
        except ValueError:
            raise ValueError("mirror upstream carries an invalid port") from None
        if _port is not None and not 0 < _port <= 65535:
            raise ValueError("mirror upstream port is out of range")
        return value


class GatedNetworkPolicy(BaseModel):
    """Explicit operator config for gated windows. All numbers are required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: NetworkBudget
    default_target_caps: WindowCaps
    default_install_caps: WindowCaps
    registry: PinnedRegistry = Field(default_factory=PinnedRegistry)
    install_proxy: InstallProxyConfig | None = None

    @model_validator(mode="after")
    def _purpose_match(self) -> GatedNetworkPolicy:
        if self.default_target_caps.purpose is not NetworkPurpose.TARGET:
            raise ValueError("default_target_caps must carry purpose target")
        if self.default_install_caps.purpose is not NetworkPurpose.INSTALL:
            raise ValueError("default_install_caps must carry purpose install")
        return self

    def caps_for(self, purpose: NetworkPurpose) -> WindowCaps:
        """Default caps for one purpose (explicit per-purpose pools stay separate)."""
        if purpose is NetworkPurpose.TARGET:
            return self.default_target_caps
        return self.default_install_caps
