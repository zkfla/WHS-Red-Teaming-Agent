"""Preserve the address identity carried by an Agent target."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from redteam.core.contracts.target import AnyTarget, UrlTarget
from redteam.core.contracts.tool import ToolTargetBinding
from redteam.core.provider_contracts import OperationTargetBinding, operation_target_binding


@dataclass(frozen=True)
class NetworkIdentity:
    host: str
    port: int | None
    pinned: bool

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.pinned else host


def network_identity(target: AnyTarget) -> NetworkIdentity | None:
    if isinstance(target, UrlTarget):
        parsed = urlsplit(target.url)
        if parsed.hostname is None or parsed.username is not None or parsed.password is not None:
            raise ValueError("target URL must have an address without credentials")
        return NetworkIdentity(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            parsed.port is not None,
        )
    address = getattr(target, "host", None) or getattr(target, "domain", None)
    if not address:
        return None
    # Raw IPv6 hosts have no port; bracketed IPv6 may carry one.
    raw = str(address)
    parsed = urlsplit("//" + (f"[{raw}]" if raw.count(":") > 1 and "[" not in raw else raw))
    if parsed.hostname is None or parsed.username is not None or parsed.path:
        raise ValueError("target host must be an address")
    explicit = getattr(target, "port", None)
    if explicit is not None and parsed.port is not None and explicit != parsed.port:
        raise ValueError("target contains conflicting ports")
    port = explicit if explicit is not None else parsed.port
    return NetworkIdentity(parsed.hostname, port, port is not None)


def validate_network_arguments(
    target: AnyTarget,
    arguments: dict,
    *,
    operation: str = "",
    bindings: Mapping[str, OperationTargetBinding] | None = None,
    argument_bindings: Mapping[str, str] | None = None,
    target_binding: ToolTargetBinding | None = None,
) -> None:
    binding = (
        target_binding
        if target_binding is not None
        else operation_target_binding(operation, bindings)
    )
    if binding.requires_url_target and not isinstance(target, UrlTarget):
        raise ValueError(f"{operation} requires a known URL target")
    identity = network_identity(target)
    if identity is None:
        return
    declared = argument_bindings or {}

    def fields(names):
        return set(names) | {remote for remote, common in declared.items() if common in names}

    ports = [arguments[name] for name in fields(("port",)) if arguments.get(name) is not None]
    for value in ports:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("tool argument port is invalid")
        if identity.port is not None and int(value) != identity.port:
            raise ValueError("tool argument port differs from proposal target")
    if ports and any(int(value) != int(ports[0]) for value in ports):
        raise ValueError("tool arguments contain conflicting ports")
    for field in fields(("host", "domain", "hostname")):
        if field not in arguments:
            continue
        actual = network_identity_from_address(str(arguments[field]))
        if (
            actual.host != identity.host
            or (actual.port is not None and actual.port != identity.port)
            or (identity.pinned and actual.port is None and not ports)
        ):
            raise ValueError("tool argument address differs from proposal target")
    for field in fields(("url", "base_url", "target_url")):
        value = arguments.get(field)
        if not isinstance(value, str):
            continue
        if (
            value.startswith("/")
            and not value.startswith("//")
            and not binding.requires_exact_url_match
        ):
            continue
        parsed = urlsplit(value if "://" in value or value.startswith("//") else "//" + value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.hostname != identity.host
        ):
            raise ValueError("tool argument URL differs from proposal target")
        if identity.port is not None and port != identity.port:
            raise ValueError("tool argument port differs from proposal target")
        if isinstance(target, UrlTarget):
            expected = urlsplit(target.url)
            if parsed.scheme and parsed.scheme != expected.scheme:
                raise ValueError("tool argument scheme differs from proposal target")
            if binding.requires_exact_url_match and (
                parsed.scheme != expected.scheme
                or (parsed.path or "/") != (expected.path or "/")
                or parsed.query != expected.query
            ):
                raise ValueError(f"{operation} URL differs from the known resource")
    network_values = {"host", "domain", "hostname", "url", "base_url", "target_url", "port"}
    for remote, common in declared.items():
        if (
            common not in network_values
            and remote in arguments
            and common in arguments
            and arguments[remote] != arguments[common]
        ):
            raise ValueError("tool arguments contain conflicting target values")


def network_identity_from_address(address: str) -> NetworkIdentity:
    from redteam.core.contracts.target import HostTarget

    identity = network_identity(HostTarget(host=address))
    if identity is None:
        raise ValueError("tool argument address is missing")
    return identity
