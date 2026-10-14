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
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from redteam.capability.shell_policy import validate_pinned_image
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

    @field_validator("endpoint", "mirror_upstream")
    @classmethod
    def _http_endpoint(cls, value: str) -> str:
        """Only an origin: no credentials, hidden controls or ignored suffix."""
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("install route must be a control-free http(s) origin")
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ValueError("install route carries an invalid origin or port") from None
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or "?" in value or "#" in value):
            raise ValueError("install route must be a credential-free http(s) origin")
        if port is not None and not 0 < port <= 65535:
            raise ValueError("install route port is out of range")
        return value

    @field_validator("pinned_hosts")
    @classmethod
    def _canonical_pins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            try:
                canonical = str(ipaddress.ip_address(value))
            except ValueError:
                raise ValueError("proxy pins must be canonical IP literals") from None
            if canonical != value:
                raise ValueError("proxy pins must be canonical IP literals")
        return values


class GatedNetworkPolicy(BaseModel):
    """Explicit operator config for gated windows. All numbers are required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: NetworkBudget
    default_target_caps: WindowCaps
    default_install_caps: WindowCaps
    registry: PinnedRegistry = Field(default_factory=PinnedRegistry)
    install_proxy: InstallProxyConfig | None = None
    # Missing legacy selections can still be inspected, but never open a gate.
    sidecar_image: str | None = Field(default=None, min_length=1, max_length=256)
    # Direct addresses only: no implicit translation of host loopback or ports.
    target_network_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target_ports: dict[str, tuple[Annotated[int, Field(strict=True, ge=1, le=65535)], ...]] = Field(
        default_factory=dict,
    )

    @model_serializer(mode="wrap")
    def compatible_route_payload(self, handler):
        value = handler(self)
        if self.target_network_id is None:
            value.pop("target_network_id", None)
        if not self.target_ports:
            value.pop("target_ports", None)
        return value

    @model_validator(mode="after")
    def valid_target_ports(self):
        for name, ports in self.target_ports.items():
            if name not in self.registry.targets or not ports:
                raise ValueError("target_ports must select existing target pins")
            if any(isinstance(port, bool) or not 1 <= port <= 65535 for port in ports):
                raise ValueError("target port must be 1..65535")
        return self

    def require_sidecar_image(self) -> str:
        if self.sidecar_image is None:
            raise ValueError("gated_sidecar_image_required")
        try:
            validate_pinned_image(self.sidecar_image)
        except ValueError:
            raise ValueError("gated_sidecar_image_unpinned") from None
        return self.sidecar_image

    @model_validator(mode="after")
    def _purpose_match(self) -> GatedNetworkPolicy:
        if self.default_target_caps.purpose is not NetworkPurpose.TARGET:
            raise ValueError("default_target_caps must carry purpose target")
        if self.default_install_caps.purpose is not NetworkPurpose.INSTALL:
            raise ValueError("default_install_caps must carry purpose install")
        if self.install_proxy is not None and (
            set(self.install_proxy.pinned_hosts) & set(self.registry.target_ips())
        ):
            raise ValueError("proxy and target pinned IPs must be disjoint")
        return self

    def caps_for(self, purpose: NetworkPurpose) -> WindowCaps:
        """Default caps for one purpose (explicit per-purpose pools stay separate)."""
        if purpose is NetworkPurpose.TARGET:
            return self.default_target_caps
        return self.default_install_caps
