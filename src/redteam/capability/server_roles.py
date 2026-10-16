"""Provider-owned MCP server role tables.

The launcher role list from servers __main__ and the deployment roles
plus tool bindings from deployment live here as one table. Deployment
keeps its public names as aliases over this data; the app synchronizer
can switch its import source without any behavior change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerRoleSpec:
    """One launchable role: connection defaults plus tool bindings."""

    role: str
    kind: str
    transport: str
    risk_level: str
    max_concurrency: int
    max_output_bytes: int
    forwards_session_credentials: bool
    operations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Only verified local deployment assembly may enable this result contract.
    supports_execution_termination: bool = False


BUILTIN_SERVER_ROLES: tuple[ServerRoleSpec, ...] = (
    ServerRoleSpec(
        role="web-http",
        kind="web_api",
        transport="stdio",
        risk_level="high",
        max_concurrency=1,
        max_output_bytes=262144,
        forwards_session_credentials=True,
        supports_execution_termination=True,
        operations={
            "session_list": ("response_analysis",),
            "http_request": ("http_request",),
            "http_compare_sessions": ("response_analysis",),
            "http_security_posture": ("response_analysis",),
            "http_compare_anonymous": ("response_analysis",),
        },
    ),
    ServerRoleSpec(
        role="recon-passive",
        kind="recon",
        transport="stdio",
        risk_level="medium",
        max_concurrency=1,
        max_output_bytes=262144,
        forwards_session_credentials=False,
        supports_execution_termination=True,
        operations={
            "dns_resolve": ("service_discovery",),
            "http_probe": ("service_discovery",),
            "tls_inspect": ("service_discovery",),
            "openapi_inventory": ("endpoint_discovery",),
            "oidc_inventory": ("endpoint_discovery",),
            "html_inventory": ("endpoint_discovery",),
        },
    ),
)

BUILTIN_LAUNCHER_ROLES: tuple[str, ...] = (
    "web-http",
    "web-proxy",
    "recon-passive",
    "recon-active",
    "web-zap",
    "web-nuclei",
)

_ROLE_SPECS = {spec.role: spec for spec in BUILTIN_SERVER_ROLES}


def launcher_role_names() -> tuple[str, ...]:
    """Role names the stdio launcher accepts, in a stable order."""

    return BUILTIN_LAUNCHER_ROLES


def deployment_role_names() -> tuple[str, ...]:
    """Role names the operator deployment flow provisions."""

    return tuple(spec.role for spec in BUILTIN_SERVER_ROLES)


def role_spec(role: str) -> ServerRoleSpec | None:
    """The deployment spec for one role, or None when unknown."""

    return _ROLE_SPECS.get(role)


def role_operations(role: str) -> dict[str, list[str]]:
    """Plain tool-to-operations copy for one deployment role."""

    spec = _ROLE_SPECS.get(role)
    if spec is None:
        raise KeyError(f"unknown server role: {role}")
    return {name: list(operations) for name, operations in spec.operations.items()}
