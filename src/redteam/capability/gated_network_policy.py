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
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from redteam.capability.shell_policy import validate_pinned_image
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkPurpose,
    WindowCaps,
)
from redteam.core.convert import digest_payload

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


# Bounds owned by this schema. The presets below are the only UI/CLI defaults.
MAX_TARGET_DELIVERIES = 100_000
MAX_OUTPUT_BYTES = 1 << 30
MAX_DURATION_SECONDS = 3600.0
MAX_DELIVERY_RATE_PER_SECOND = 1000.0
MAX_ENDPOINTS_PER_TARGET = 32
MAX_REGISTRY_NAME_CHARS = 128
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")

TransportName = Literal["tcp", "udp"]
PresetName = Literal["probe", "standard"]


class SelectedEndpoint(BaseModel):
    """One operator-selected port and transport inside a pinned registry target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: int = Field(ge=1, le=65535, strict=True)
    transport: TransportName


class EndpointRestriction(BaseModel):
    """Immutable narrowing of original engagement authority, keyed by registry name.

    It never adds a destination, port or transport. The owning policy checks that
    every key is one of its pinned target names. Without this restriction the
    legacy behavior applies: every pinned IP with both tcp and udp.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    selections: Mapping[str, tuple[SelectedEndpoint, ...]]

    @field_validator("selections")
    @classmethod
    def _canonical(
        cls, value: Mapping[str, tuple[SelectedEndpoint, ...]]
    ) -> Mapping[str, tuple[SelectedEndpoint, ...]]:
        if not value:
            raise ValueError("endpoint_restriction_empty")
        canonical: dict[str, tuple[SelectedEndpoint, ...]] = {}
        for name in sorted(value):
            items = value[name]
            if not name or len(name) > MAX_REGISTRY_NAME_CHARS:
                raise ValueError("endpoint_restriction_name_invalid")
            if not items:
                raise ValueError("endpoint_restriction_target_empty")
            if len(items) > MAX_ENDPOINTS_PER_TARGET:
                raise ValueError("endpoint_restriction_too_many")
            ordered = tuple(sorted(items, key=lambda item: (item.port, item.transport)))
            if len({(item.port, item.transport) for item in ordered}) != len(ordered):
                raise ValueError("endpoint_restriction_duplicate")
            canonical[name] = ordered
        # Read-only view over a private dict that no caller can reach.
        return MappingProxyType(canonical)

    @field_serializer("selections")
    def _plain_selections(
        self, value: Mapping[str, tuple[SelectedEndpoint, ...]]
    ) -> dict[str, tuple[SelectedEndpoint, ...]]:
        # A plain dict keeps the serialized shape identical to the earlier mutable field.
        return dict(value)

    def protocols_for(self, name: str, port: int) -> tuple[str, ...] | None:
        """Selected transports for one registry name and port, or None when not selected."""
        items = self.selections.get(name, ())
        protocols = tuple(sorted({item.transport for item in items if item.port == port}))
        return protocols or None


class _GatedPolicyCore(BaseModel):
    """Fields shared by a resolved policy and an unresolved preparation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: NetworkBudget
    default_target_caps: WindowCaps
    default_install_caps: WindowCaps
    registry: PinnedRegistry = Field(default_factory=PinnedRegistry)
    install_proxy: InstallProxyConfig | None = None
    endpoint_restriction: EndpointRestriction | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_restriction(self, handler):
        # Legacy policies keep their historical serialized bytes and content identity.
        result = handler(self)
        if self.endpoint_restriction is None:
            result.pop("endpoint_restriction", None)
        return result

    @model_validator(mode="after")
    def _purpose_match(self) -> _GatedPolicyCore:
        if self.default_target_caps.purpose is not NetworkPurpose.TARGET:
            raise ValueError("default_target_caps must carry purpose target")
        if self.default_install_caps.purpose is not NetworkPurpose.INSTALL:
            raise ValueError("default_install_caps must carry purpose install")
        if self.install_proxy is not None and (
            set(self.install_proxy.pinned_hosts) & set(self.registry.target_ips())
        ):
            raise ValueError("proxy and target pinned IPs must be disjoint")
        if self.endpoint_restriction is not None and (
            set(self.endpoint_restriction.selections) - set(self.registry.targets)
        ):
            raise ValueError("endpoint_restriction_unknown_target")
        return self

    def caps_for(self, purpose: NetworkPurpose) -> WindowCaps:
        """Default caps for one purpose (explicit per-purpose pools stay separate)."""
        if purpose is NetworkPurpose.TARGET:
            return self.default_target_caps
        return self.default_install_caps


class GatedNetworkPolicy(_GatedPolicyCore):
    """Resolved operator policy for gated windows. Inspectable without a sidecar."""

    # Missing legacy selections can still be inspected, but never open a gate.
    sidecar_image: str | None = Field(default=None, min_length=1, max_length=256)

    def require_sidecar_image(self) -> str:
        if self.sidecar_image is None:
            raise ValueError("gated_sidecar_image_required")
        try:
            validate_pinned_image(self.sidecar_image)
        except ValueError:
            raise ValueError("gated_sidecar_image_unpinned") from None
        return self.sidecar_image


class ManagedSidecarRequest(BaseModel):
    """Explicit operator request to build the sidecar from the source-owned recipe.

    It carries no image identity. Preparation builds and inspects the image, then calls
    resolve_managed_sidecar. This module never builds, pulls or inspects anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recipe: Literal["gate-sidecar"] = "gate-sidecar"


class GatedNetworkPolicyRequest(_GatedPolicyCore):
    """Unresolved preparation request. Inspectable, but it can never open a gate."""

    # New preparation always carries an explicit selection. Only loaded legacy policies may omit it.
    endpoint_restriction: EndpointRestriction
    managed_sidecar: ManagedSidecarRequest

    def require_sidecar_image(self) -> str:
        # Raised before any reservation when a window open reaches this request.
        raise ValueError("gated_sidecar_unresolved")


class GatedLimits(BaseModel):
    """Operator-selected finite limits: one target purse and each target window."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_flow_init_deliveries: int = Field(ge=1, le=MAX_TARGET_DELIVERIES, strict=True)
    max_output_bytes: int = Field(ge=1, le=MAX_OUTPUT_BYTES, strict=True)
    duration_seconds: float = Field(gt=0, le=MAX_DURATION_SECONDS)
    delivery_rate_per_second: float | None = Field(
        default=None, gt=0, le=MAX_DELIVERY_RATE_PER_SECOND
    )


SUPPORTED_GATED_PRESETS: Mapping[PresetName, GatedLimits] = MappingProxyType(
    {
        "probe": GatedLimits(
            max_flow_init_deliveries=256,
            max_output_bytes=4 * 1024 * 1024,
            duration_seconds=300.0,
        ),
        "standard": GatedLimits(
            max_flow_init_deliveries=4096,
            max_output_bytes=256 * 1024 * 1024,
            duration_seconds=900.0,
        ),
    }
)


def _is_canonical_ipv4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(ipaddress.IPv4Address(value)) == value
    except ValueError:
        return False


def _pinned_registry(targets: Mapping[str, Sequence[str]]) -> PinnedRegistry:
    if not targets:
        raise ValueError("gated_target_pins_required")
    pins: dict[str, tuple[str, ...]] = {}
    for name, ips in targets.items():
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or len(name) > MAX_REGISTRY_NAME_CHARS
        ):
            raise ValueError("gated_target_name_invalid")
        if isinstance(ips, str) or not ips:
            raise ValueError("gated_target_pin_required")
        if not all(_is_canonical_ipv4(ip) for ip in ips):
            raise ValueError("gated_pin_not_canonical_ipv4")
        pins[name] = tuple(ips)
    return PinnedRegistry(targets=pins, mirrors={})


def _restriction(endpoints: Mapping[str, Sequence[tuple[int, str]]]) -> EndpointRestriction:
    # The schema validates each port and transport, so the transport allowlist has one owner.
    return EndpointRestriction(
        selections={
            name: tuple(
                SelectedEndpoint.model_validate({"port": port, "transport": transport})
                for port, transport in items
            )
            for name, items in endpoints.items()
        }
    )


def _copy_restriction(restriction: EndpointRestriction) -> EndpointRestriction:
    """A new restriction object, so a request and its resolved policy never share a mapping."""
    return EndpointRestriction(
        selections={name: tuple(items) for name, items in restriction.selections.items()}
    )


def _require_endpoint_selection(
    endpoints: Mapping[str, Sequence[tuple[int, str]]] | None, registry: PinnedRegistry
) -> None:
    """New prepare never yields a broad policy: every pinned target needs its own selection."""
    if not endpoints:
        raise ValueError("gated_endpoint_selection_required")
    if set(endpoints) - set(registry.targets):
        raise ValueError("endpoint_restriction_unknown_target")
    if set(registry.targets) - set(endpoints):
        raise ValueError("gated_endpoint_selection_missing_target")


def build_gated_network_policy(
    *,
    targets: Mapping[str, Sequence[str]],
    sidecar: str | ManagedSidecarRequest,
    preset: PresetName | None = None,
    limits: GatedLimits | None = None,
    endpoints: Mapping[str, Sequence[tuple[int, str]]],
) -> GatedNetworkPolicy | GatedNetworkPolicyRequest:
    """Assemble one finite gated policy. Pure: no build, DNS, request or policy write.

    Exactly one of preset or limits is required. Install capacity is zero, with no
    mirrors and no install proxy. A pinned sidecar yields a resolved GatedNetworkPolicy;
    an explicit ManagedSidecarRequest yields an unresolved GatedNetworkPolicyRequest.
    endpoints is required and must select every pinned target explicitly. It narrows the
    pinned targets only and adds no destination, port or transport.
    """
    if (preset is None) == (limits is None):
        raise ValueError("gated_limits_selection_required")
    resolved: GatedLimits
    if preset is not None:
        if preset not in SUPPORTED_GATED_PRESETS:
            raise ValueError("gated_preset_unsupported")
        resolved = SUPPORTED_GATED_PRESETS[preset]
    elif limits is None:
        raise ValueError("gated_limits_selection_required")
    else:
        resolved = limits
    registry = _pinned_registry(targets)
    _require_endpoint_selection(endpoints, registry)
    target_capacity = NetworkCapacity(
        max_flow_init_deliveries=resolved.max_flow_init_deliveries,
        max_output_bytes=resolved.max_output_bytes,
    )
    budget = NetworkBudget(
        global_capacity=target_capacity,
        target_capacity=target_capacity,
        install_capacity=NetworkCapacity(max_flow_init_deliveries=0, max_output_bytes=0),
        delivery_rate_per_second=resolved.delivery_rate_per_second,
    )
    target_caps = WindowCaps(
        max_flow_init_deliveries=resolved.max_flow_init_deliveries,
        max_output_bytes=resolved.max_output_bytes,
        delivery_rate_per_second=resolved.delivery_rate_per_second,
        duration_seconds=resolved.duration_seconds,
        purpose=NetworkPurpose.TARGET,
    )
    # WindowCaps requires positive values. The zero install budget makes them unusable.
    install_caps = WindowCaps(
        max_flow_init_deliveries=1,
        max_output_bytes=1,
        duration_seconds=1.0,
        purpose=NetworkPurpose.INSTALL,
    )
    restriction = _restriction(endpoints)
    if isinstance(sidecar, ManagedSidecarRequest):
        return GatedNetworkPolicyRequest(
            budget=budget,
            default_target_caps=target_caps,
            default_install_caps=install_caps,
            registry=registry,
            install_proxy=None,
            endpoint_restriction=restriction,
            managed_sidecar=sidecar,
        )
    if not isinstance(sidecar, str):
        raise ValueError("gated_sidecar_selection_invalid")
    try:
        validate_pinned_image(sidecar)
    except ValueError:
        raise ValueError("gated_sidecar_image_unpinned") from None
    return GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target_caps,
        default_install_caps=install_caps,
        registry=registry,
        install_proxy=None,
        endpoint_restriction=restriction,
        sidecar_image=sidecar,
    )


def resolve_managed_sidecar(
    request: GatedNetworkPolicyRequest, image_id: str
) -> GatedNetworkPolicy:
    """Pure request-to-policy transition for an image that preparation already inspected.

    Only an immutable sha256 image ID is accepted. The budget, caps and install proxy keep
    their values. The registry and endpoint restriction are fresh copies, so the resolved
    policy never shares a mutable mapping with the request.
    """
    if not isinstance(request, GatedNetworkPolicyRequest):
        raise ValueError("gated_sidecar_resolution_requires_request")
    if not _IMAGE_ID.fullmatch(image_id):
        raise ValueError("gated_sidecar_resolution_requires_image_id")
    return GatedNetworkPolicy(
        budget=request.budget,
        default_target_caps=request.default_target_caps,
        default_install_caps=request.default_install_caps,
        registry=request.registry.model_copy(deep=True),
        install_proxy=request.install_proxy,
        endpoint_restriction=_copy_restriction(request.endpoint_restriction),
        sidecar_image=image_id,
    )


def policy_content_hash(policy: GatedNetworkPolicy | GatedNetworkPolicyRequest) -> str:
    """Canonical content identity of a policy. The endpoint restriction is included."""
    return digest_payload(policy.model_dump(mode="json"))
