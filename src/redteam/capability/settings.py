"""Environment-backed settings for configured MCP server IDs.

``CapabilityConfig.mcp_servers`` remains the shared list of server IDs.
Endpoint, capability kind, risk, and secret indirection stay inside the
Capability implementation so the frozen Core config contract need not change.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from redteam.core.enums import RiskLevel

_SAFE_SERVER_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CANONICAL_OPERATION = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "host.docker.internal"}


class MCPConfigurationError(ValueError):
    pass


class MCPServerKind(str, Enum):
    WEB_API = "web_api"
    MOBILE = "mobile"
    RECON = "recon"
    ANALYSIS = "analysis"


class MCPTransportKind(str, Enum):
    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"


@dataclass(frozen=True, slots=True)
class MCPServerSettings:
    server_id: str
    kind: MCPServerKind
    transport: MCPTransportKind
    endpoint: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.LOW
    bearer_token: str | None = field(default=None, repr=False)
    authorization_value: str | None = field(default=None, repr=False)
    process_environment: dict[str, str] = field(default_factory=dict, repr=False)
    allowed_tools: tuple[str, ...] | None = None
    tool_operations: dict[str, tuple[str, ...]] | None = None
    max_concurrency: int = 1
    max_output_bytes: int = 1_048_576
    profile: str | None = None
    workspace_asset_ref: str | None = None
    vdb_asset_ref: str | None = None
    page_text_config_path: str | None = None
    page_text_config_sha256: str | None = None
    execution_asset_ref: str | None = None
    execution_policy_path: str | None = None
    execution_policy_sha256: str | None = None

    @classmethod
    def from_environment(cls, server_id: str) -> MCPServerSettings:
        prefix = environment_prefix(server_id)
        kind_value = _required_environment_value(f"{prefix}_KIND")
        transport_value = os.environ.get(
            f"{prefix}_TRANSPORT", MCPTransportKind.STREAMABLE_HTTP.value
        )
        risk_value = os.environ.get(f"{prefix}_RISK_LEVEL", RiskLevel.LOW.value)

        try:
            kind = MCPServerKind(kind_value)
        except ValueError as exc:
            supported = ", ".join(kind.value for kind in MCPServerKind)
            raise MCPConfigurationError(f"{prefix}_KIND must be one of: {supported}") from exc
        try:
            transport = MCPTransportKind(transport_value)
        except ValueError as exc:
            supported = ", ".join(transport.value for transport in MCPTransportKind)
            raise MCPConfigurationError(f"{prefix}_TRANSPORT must be one of: {supported}") from exc
        try:
            risk_level = RiskLevel(risk_value)
        except ValueError as exc:
            raise MCPConfigurationError(f"invalid risk level {risk_value!r}") from exc

        endpoint: str | None = None
        command: str | None = None
        args: tuple[str, ...] = ()
        token: str | None = None
        authorization_value: str | None = None
        process_environment: dict[str, str] = {}

        if transport == MCPTransportKind.STREAMABLE_HTTP:
            endpoint = _required_environment_value(f"{prefix}_ENDPOINT")
            _validate_endpoint(endpoint, prefix)
            token = _load_bearer_token(prefix)
            authorization_value = _load_authorization_value(prefix)
            if token is not None and authorization_value is not None:
                raise MCPConfigurationError(
                    f"{prefix}_TOKEN_ENV_VAR and {prefix}_AUTHORIZATION_ENV_VAR "
                    "are mutually exclusive"
                )
        else:
            command = _required_environment_value(f"{prefix}_COMMAND")
            _validate_command(command, prefix)
            args = _load_stdio_arguments(prefix)
            process_environment = _load_process_environment(prefix)

        allowed_tools = _load_allowed_tools(prefix)
        tool_operations = _load_tool_operations(prefix)
        max_concurrency = _load_max_concurrency(prefix)
        max_output_bytes = _load_max_output_bytes(prefix)
        profile = os.environ.get(f"{prefix}_PROFILE")
        workspace_asset_ref = None
        vdb_asset_ref = None
        page_text_config_path = None
        page_text_config_sha256 = None
        execution_asset_ref = None
        execution_policy_path = None
        execution_policy_sha256 = None
        if profile in {"vdb_readonly", "vdb_readonly_extended"}:
            if kind != MCPServerKind.ANALYSIS or transport != MCPTransportKind.STDIO:
                raise MCPConfigurationError("vdb profile requires analysis kind and stdio")
            vdb_asset_ref = _required_environment_value(f"{prefix}_VDB_ASSET_REF")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", vdb_asset_ref):
                raise MCPConfigurationError("vdb asset_ref must be an opaque identifier")
            from redteam.capability.vdb_manifest import vdb_specs

            specs = vdb_specs(profile)
            if (
                allowed_tools is None or set(allowed_tools) != set(specs)
                or tool_operations != {
                    name: (spec.operation,) for name, spec in specs.items()
                }
            ):
                raise MCPConfigurationError("vdb profile requires exact allowlist and bindings")
            if risk_level == RiskLevel.LOW:
                raise MCPConfigurationError("vdb profile requires at least medium risk")
        elif profile == "page_text_readonly":
            if kind != MCPServerKind.WEB_API or transport != MCPTransportKind.STDIO:
                raise MCPConfigurationError("page text profile requires web_api kind and stdio")
            if f"{prefix}_RISK_LEVEL" not in os.environ:
                raise MCPConfigurationError("page text profile requires an explicit risk level")
            from redteam.capability.servers.page_text_mcp import (
                MODULE,
                PROPOSED_OPERATION,
                REMOTE_TOOL_NAME,
                load_page_text_config,
            )

            page_text_config_path = str(
                Path(_required_environment_value(f"{prefix}_PAGE_TEXT_CONFIG")).resolve()
            )
            try:
                _, page_text_config_sha256 = load_page_text_config(page_text_config_path)
            except ValueError:
                raise MCPConfigurationError("invalid page text operator configuration") from None
            expected_tools = (REMOTE_TOOL_NAME,)
            expected_bindings: dict[str, tuple[str, ...]] = {
                REMOTE_TOOL_NAME: (PROPOSED_OPERATION,)
            }
            if allowed_tools is not None and allowed_tools != expected_tools:
                raise MCPConfigurationError("page text profile requires its exact tool allowlist")
            if tool_operations is not None and tool_operations != expected_bindings:
                raise MCPConfigurationError(
                    "page text profile requires its exact operation binding"
                )
            if args:
                raise MCPConfigurationError("page text profile constructs its own stdio arguments")
            allowed_tools = expected_tools
            tool_operations = expected_bindings
            args = (
                "-m", MODULE, "--config", page_text_config_path,
                "--sha256", page_text_config_sha256,
            )
        elif profile == "execution":
            if kind != MCPServerKind.ANALYSIS or transport != MCPTransportKind.STDIO:
                raise MCPConfigurationError("execution profile requires analysis kind and stdio")
            if f"{prefix}_RISK_LEVEL" not in os.environ:
                raise MCPConfigurationError("execution profile requires an explicit risk level")
            from redteam.capability.execution_manifest import (
                EXECUTION_TOOLS,
                minimum_execution_risk,
            )
            from redteam.capability.servers.execution import (
                MODULE,
                load_execution_policy,
            )

            execution_policy_path = str(
                Path(_required_environment_value(f"{prefix}_EXECUTION_POLICY")).resolve()
            )
            try:
                execution_policy, execution_policy_sha256 = load_execution_policy(
                    execution_policy_path
                )
            except (OSError, ValueError):
                raise MCPConfigurationError("invalid execution operator policy") from None
            execution_asset_ref = execution_policy.asset_ref
            expected_tools = tuple(EXECUTION_TOOLS)
            expected_bindings = {
                name: (spec.operation,) for name, spec in EXECUTION_TOOLS.items()
            }
            if allowed_tools is not None and allowed_tools != expected_tools:
                raise MCPConfigurationError("execution profile requires its exact tool allowlist")
            if tool_operations is not None and tool_operations != expected_bindings:
                raise MCPConfigurationError(
                    "execution profile requires its exact operation bindings"
                )
            if args:
                raise MCPConfigurationError("execution profile constructs its own stdio arguments")
            risk_order = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
            if risk_order.index(risk_level) < risk_order.index(minimum_execution_risk()):
                raise MCPConfigurationError("execution profile risk cannot be below the minimum")
            allowed_tools = expected_tools
            tool_operations = expected_bindings
            args = (
                "-m",
                MODULE,
                "--policy",
                execution_policy_path,
                "--sha256",
                execution_policy_sha256,
            )
        elif profile is not None:
            if profile not in {"workspace_read", "workspace_write"}:
                raise MCPConfigurationError(f"{prefix}_PROFILE is not a supported profile")
            if kind != MCPServerKind.ANALYSIS or transport != MCPTransportKind.STDIO:
                raise MCPConfigurationError("workspace profiles require analysis kind and stdio")
            workspace_asset_ref = _required_environment_value(f"{prefix}_WORKSPACE_ASSET_REF")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", workspace_asset_ref):
                raise MCPConfigurationError("workspace asset_ref must be an opaque identifier")
            # Import here: workspace manifest imports settings-adjacent contracts.
            from redteam.capability.workspace_manifest import (
                minimum_workspace_risk,
                workspace_specs,
            )

            specs = workspace_specs(profile)
            if (
                allowed_tools is None
                or set(allowed_tools) != set(specs)
                or tool_operations != {name: (spec.operation,) for name, spec in specs.items()}
            ):
                raise MCPConfigurationError(
                    "workspace profile requires exact allowlist and bindings"
                )
            risk_order = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
            if risk_order.index(risk_level) < risk_order.index(minimum_workspace_risk(profile)):
                raise MCPConfigurationError("workspace profile risk cannot be below the minimum")
        if allowed_tools is not None and tool_operations is not None:
            allowed_names = set(allowed_tools)
            binding_names = set(tool_operations)
            if binding_names != allowed_names:
                raise MCPConfigurationError(
                    f"{prefix}_TOOL_OPERATIONS_JSON keys must exactly match "
                    f"{prefix}_ALLOWED_TOOLS_JSON"
                )

        return cls(
            server_id=server_id,
            kind=kind,
            transport=transport,
            endpoint=endpoint,
            command=command,
            args=args,
            risk_level=risk_level,
            bearer_token=token,
            authorization_value=authorization_value,
            process_environment=process_environment,
            allowed_tools=allowed_tools,
            tool_operations=tool_operations,
            max_concurrency=max_concurrency,
            max_output_bytes=max_output_bytes,
            profile=profile,
            workspace_asset_ref=workspace_asset_ref,
            vdb_asset_ref=vdb_asset_ref,
            page_text_config_path=page_text_config_path,
            page_text_config_sha256=page_text_config_sha256,
            execution_asset_ref=execution_asset_ref,
            execution_policy_path=execution_policy_path,
            execution_policy_sha256=execution_policy_sha256,
        )

    def request_headers(self) -> dict[str, str]:
        if self.authorization_value is not None:
            return {"Authorization": self.authorization_value}
        if self.bearer_token is None:
            return {}
        return {"Authorization": f"Bearer {self.bearer_token}"}


def mcp_server_ids_from_environment(env_var: str = "REDTEAM_MCP_SERVERS") -> list[str]:
    """Which MCP servers to activate -- the CLI's only source for
    ``capability.mcp_servers`` (there is no application-config file).

    A comma-separated list of server ids, e.g. ``REDTEAM_MCP_SERVERS=recon,web-http``
    -- each id still resolves its own connection details (endpoint/transport/
    credential) via ``MCPServerSettings.from_environment``. Unset or blank
    means no server: the caller gets an honest empty tool catalog, never an
    automatic fallback.
    """
    raw = os.environ.get(env_var, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def environment_prefix(server_id: str) -> str:
    if not _SAFE_SERVER_ID.fullmatch(server_id):
        raise MCPConfigurationError("MCP server IDs must use lowercase kebab-case")
    return f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise MCPConfigurationError(f"required environment variable {name} is not set")
    return value.strip()


def _load_bearer_token(prefix: str) -> str | None:
    token_env_var = os.environ.get(f"{prefix}_TOKEN_ENV_VAR")
    if not token_env_var:
        return None
    if not _SAFE_ENVIRONMENT_NAME.fullmatch(token_env_var):
        raise MCPConfigurationError(
            f"{prefix}_TOKEN_ENV_VAR is not a valid environment variable name"
        )
    token = os.environ.get(token_env_var)
    if not token:
        raise MCPConfigurationError(f"secret environment variable {token_env_var!r} is not set")
    return token


def _load_authorization_value(prefix: str) -> str | None:
    """Load an exact Authorization value through secret-name indirection.

    Some MCP servers, including ZAP's official MCP add-on, require a raw
    security key rather than a Bearer token.  The value remains outside app
    configuration and descriptor metadata, just like ``TOKEN_ENV_VAR``.
    """

    value_env_var = os.environ.get(f"{prefix}_AUTHORIZATION_ENV_VAR")
    if not value_env_var:
        return None
    if not _SAFE_ENVIRONMENT_NAME.fullmatch(value_env_var):
        raise MCPConfigurationError(
            f"{prefix}_AUTHORIZATION_ENV_VAR is not a valid environment variable name"
        )
    value = os.environ.get(value_env_var)
    if not value:
        raise MCPConfigurationError(
            f"secret environment variable {value_env_var!r} is not set"
        )
    if "\r" in value or "\n" in value:
        raise MCPConfigurationError("Authorization value must not contain line breaks")
    return value


def _load_stdio_arguments(prefix: str) -> tuple[str, ...]:
    raw_arguments = os.environ.get(f"{prefix}_ARGS_JSON", "[]")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError(f"{prefix}_ARGS_JSON must be a JSON array of strings") from exc
    if not isinstance(arguments, list) or any(
        not isinstance(argument, str) or "\x00" in argument for argument in arguments
    ):
        raise MCPConfigurationError(
            f"{prefix}_ARGS_JSON must be a JSON array of strings without NUL bytes"
        )
    return tuple(arguments)


def _load_allowed_tools(prefix: str) -> tuple[str, ...] | None:
    value = os.environ.get(f"{prefix}_ALLOWED_TOOLS_JSON")
    if value is None:
        return None
    try:
        names = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError("ALLOWED_TOOLS_JSON must be a JSON array") from exc
    if (
        not isinstance(names, list) or not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(names) != len(set(names))
    ):
        raise MCPConfigurationError("ALLOWED_TOOLS_JSON must contain unique non-empty tool names")
    return tuple(names)


def _load_tool_operations(prefix: str) -> dict[str, tuple[str, ...]] | None:
    value = os.environ.get(f"{prefix}_TOOL_OPERATIONS_JSON")
    if value is None:
        return None
    try:
        raw_bindings = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError("TOOL_OPERATIONS_JSON must be a JSON object") from exc
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise MCPConfigurationError("TOOL_OPERATIONS_JSON must be a non-empty JSON object")

    bindings: dict[str, tuple[str, ...]] = {}
    for tool_name, operations in raw_bindings.items():
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise MCPConfigurationError(
                "TOOL_OPERATIONS_JSON keys must be non-empty remote tool names"
            )
        if (
            not isinstance(operations, list)
            or any(
                not isinstance(operation, str)
                or not _CANONICAL_OPERATION.fullmatch(operation)
                for operation in operations
            )
            or len(operations) != len(set(operations))
        ):
            raise MCPConfigurationError(
                "TOOL_OPERATIONS_JSON values must be arrays of unique lower_snake_case "
                "operations"
            )
        bindings[tool_name] = tuple(operations)
    return bindings


def _load_max_concurrency(prefix: str) -> int:
    value = os.environ.get(f"{prefix}_MAX_CONCURRENCY", "1")
    try:
        limit = int(value)
    except ValueError as exc:
        raise MCPConfigurationError(f"{prefix}_MAX_CONCURRENCY must be an integer") from exc
    if not 1 <= limit <= 32:
        raise MCPConfigurationError(f"{prefix}_MAX_CONCURRENCY must be between 1 and 32")
    return limit


def _load_max_output_bytes(prefix: str) -> int:
    value = os.environ.get(f"{prefix}_MAX_OUTPUT_BYTES", "1048576")
    try:
        limit = int(value)
    except ValueError as exc:
        raise MCPConfigurationError(f"{prefix}_MAX_OUTPUT_BYTES must be an integer") from exc
    if not 1024 <= limit <= 16 * 1024 * 1024:
        raise MCPConfigurationError(
            f"{prefix}_MAX_OUTPUT_BYTES must be between 1024 and 16777216"
        )
    return limit


def _load_process_environment(prefix: str) -> dict[str, str]:
    raw_names = os.environ.get(f"{prefix}_PASSTHROUGH_ENV", "")
    names = [name.strip() for name in raw_names.split(",") if name.strip()]
    environment: dict[str, str] = {}
    for name in names:
        if not _SAFE_ENVIRONMENT_NAME.fullmatch(name):
            raise MCPConfigurationError(
                f"{prefix}_PASSTHROUGH_ENV contains an invalid environment variable name"
            )
        value = os.environ.get(name)
        if value is None:
            raise MCPConfigurationError(f"stdio environment variable {name!r} is not set")
        environment[name] = value
    return environment


def _validate_command(command: str, prefix: str) -> None:
    if "\x00" in command or "\n" in command or "\r" in command:
        raise MCPConfigurationError(f"{prefix}_COMMAND must be a single executable name or path")


def _validate_endpoint(endpoint: str, prefix: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MCPConfigurationError(f"{prefix}_ENDPOINT must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MCPConfigurationError(
            f"{prefix}_ENDPOINT must not contain credentials; use TOKEN_ENV_VAR "
            f"or AUTHORIZATION_ENV_VAR"
        )
    allow_insecure = os.environ.get(f"{prefix}_ALLOW_INSECURE_HTTP", "false").lower()
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS and allow_insecure != "true":
        raise MCPConfigurationError(
            f"{prefix}_ENDPOINT must use HTTPS outside local development; "
            f"set {prefix}_ALLOW_INSECURE_HTTP=true only for an authorized lab"
        )
