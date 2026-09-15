"""Environment-backed settings for configured MCP server IDs.

``CapabilityConfig.mcp_servers`` remains the shared list of server IDs.
Endpoint, capability kind, risk, and secret indirection stay inside the
Capability implementation so the frozen Core config contract need not change.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from redteam.core.enums import RiskLevel

_SAFE_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "host.docker.internal"}


class MCPConfigurationError(ValueError):
    pass


class MCPServerKind(str, Enum):
    WEB_API = "web_api"
    RECON = "recon"


@dataclass(frozen=True, slots=True)
class MCPServerSettings:
    server_id: str
    kind: MCPServerKind
    endpoint: str
    risk_level: RiskLevel = RiskLevel.LOW
    bearer_token: str | None = None

    @classmethod
    def from_environment(cls, server_id: str) -> MCPServerSettings:
        prefix = environment_prefix(server_id)
        endpoint = _required_environment_value(f"{prefix}_ENDPOINT")
        kind_value = _required_environment_value(f"{prefix}_KIND")
        risk_value = os.environ.get(f"{prefix}_RISK_LEVEL", RiskLevel.LOW.value)
        token_env_var = os.environ.get(f"{prefix}_TOKEN_ENV_VAR")
        token = None
        if token_env_var:
            token = os.environ.get(token_env_var)
            if not token:
                raise MCPConfigurationError(
                    f"secret environment variable {token_env_var!r} is not set"
                )

        try:
            kind = MCPServerKind(kind_value)
        except ValueError as exc:
            supported = ", ".join(kind.value for kind in MCPServerKind)
            raise MCPConfigurationError(
                f"{prefix}_KIND must be one of: {supported}"
            ) from exc
        try:
            risk_level = RiskLevel(risk_value)
        except ValueError as exc:
            raise MCPConfigurationError(f"invalid risk level {risk_value!r}") from exc

        _validate_endpoint(endpoint, prefix)
        return cls(
            server_id=server_id,
            kind=kind,
            endpoint=endpoint,
            risk_level=risk_level,
            bearer_token=token,
        )

    def request_headers(self) -> dict[str, str]:
        if self.bearer_token is None:
            return {}
        return {"Authorization": f"Bearer {self.bearer_token}"}


def environment_prefix(server_id: str) -> str:
    if not _SAFE_SERVER_ID.fullmatch(server_id):
        raise MCPConfigurationError(
            "MCP server IDs must contain only letters, digits, underscores, and hyphens"
        )
    return f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise MCPConfigurationError(f"required environment variable {name} is not set")
    return value.strip()


def _validate_endpoint(endpoint: str, prefix: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MCPConfigurationError(f"{prefix}_ENDPOINT must be an absolute http(s) URL")
    allow_insecure = os.environ.get(f"{prefix}_ALLOW_INSECURE_HTTP", "false").lower()
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS and allow_insecure != "true":
        raise MCPConfigurationError(
            f"{prefix}_ENDPOINT must use HTTPS outside local development; "
            f"set {prefix}_ALLOW_INSECURE_HTTP=true only for an authorized lab"
        )
