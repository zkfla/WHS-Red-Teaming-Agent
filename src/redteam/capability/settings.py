"""Environment-backed settings for configured MCP server IDs.

``CapabilityConfig.mcp_servers`` remains the shared list of server IDs.
Endpoint, capability kind, risk, and secret indirection stay inside the
Capability implementation so the frozen Core config contract need not change.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from pydantic import TypeAdapter, ValidationError

from redteam.capability.registration import (
    McpProfileBundle,
    ProfileSettingsContext,
    apply_profile_settings,
)
from redteam.core.action_semantics import (
    ActionNamingError,
    canonical_action_key,
    validate_operation_format,
)
from redteam.core.contracts.tool import ToolResourceBinding
from redteam.core.enums import RiskLevel

_SAFE_SERVER_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "host.docker.internal"}


_GLOBAL_SERVER_ENVIRONMENT_NAMES = frozenset({"TAVILY_API_KEY"})


def read_server_environment(
    prefix: str, *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Collect one server environment snapshot from a single place."""

    # Boundary note for the settings owner: the prefixed base comes from the
    # shared input boundary (redteam.core.environment.read_prefixed_values) over the
    # same snapshot source. Only the provider-owned extras below (secret-name
    # indirection, passthrough list, shared secret names) stay here. The
    # common loader may replace this function as long as it keeps those
    # extras and the exact key set; CLI dotenv fill still happens before.

    from redteam.core.environment import env_snapshot, read_prefixed_values

    origin = env_snapshot(source)
    scoped = read_prefixed_values(prefix, source=source)
    if prefix in origin and prefix not in scoped:
        scoped[prefix] = origin[prefix]
    for indirect in (
        scoped.get(prefix + "_TOKEN_ENV_VAR"),
        scoped.get(prefix + "_AUTHORIZATION_ENV_VAR"),
    ):
        if indirect and indirect not in scoped and indirect in origin:
            scoped[indirect] = origin[indirect]
    for name in scoped.get(prefix + "_PASSTHROUGH_ENV", "").split(","):
        name = name.strip()
        if name and name not in scoped and name in origin:
            scoped[name] = origin[name]
    for name in _GLOBAL_SERVER_ENVIRONMENT_NAMES:
        if name not in scoped and name in origin:
            scoped[name] = origin[name]
    return scoped


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
    tool_resources: dict[str, list[ToolResourceBinding]] = field(default_factory=dict)
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
    web_search_config_path: str | None = None
    web_search_config_sha256: str | None = None

    @classmethod
    def from_environment(
        cls,
        server_id: str,
        *,
        env: Mapping[str, str] | None = None,
        bundles: Mapping[str, McpProfileBundle] | None = None,
    ) -> MCPServerSettings:
        prefix = environment_prefix(server_id)
        snapshot = read_server_environment(prefix, source=env)
        kind_value = _required_environment_value(f"{prefix}_KIND", snapshot)
        transport_value = snapshot.get(
            f"{prefix}_TRANSPORT", MCPTransportKind.STREAMABLE_HTTP.value
        )
        risk_value = snapshot.get(f"{prefix}_RISK_LEVEL", RiskLevel.LOW.value)

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
            endpoint = _required_environment_value(f"{prefix}_ENDPOINT", snapshot)
            _validate_endpoint(endpoint, prefix, snapshot)
            token = _load_bearer_token(prefix, snapshot)
            authorization_value = _load_authorization_value(prefix, snapshot)
            if token is not None and authorization_value is not None:
                raise MCPConfigurationError(
                    f"{prefix}_TOKEN_ENV_VAR and {prefix}_AUTHORIZATION_ENV_VAR "
                    "are mutually exclusive"
                )
        else:
            command = _required_environment_value(f"{prefix}_COMMAND", snapshot)
            _validate_command(command, prefix)
            args = _load_stdio_arguments(prefix, snapshot)
            process_environment = _load_process_environment(prefix, snapshot)

        allowed_tools = _load_allowed_tools(prefix, snapshot)
        tool_operations = _load_tool_operations(prefix, snapshot)
        max_concurrency = _load_max_concurrency(prefix, snapshot)
        max_output_bytes = _load_max_output_bytes(prefix, snapshot)
        profile = snapshot.get(f"{prefix}_PROFILE")
        profile_context = ProfileSettingsContext(
            profile=profile,
            prefix=prefix,
            kind=kind.value,
            transport=transport.value,
            risk_level=risk_level,
            has_explicit_risk=f"{prefix}_RISK_LEVEL" in snapshot,
            allowed_tools=allowed_tools,
            tool_operations=tool_operations,
            args=args,
            process_environment=process_environment,
            env=snapshot,
        )
        if profile is not None:
            apply_profile_settings(profile_context, bundles)
        allowed_tools = profile_context.allowed_tools
        tool_operations = profile_context.tool_operations
        args = profile_context.args
        process_environment = profile_context.process_environment
        workspace_asset_ref = profile_context.workspace_asset_ref
        vdb_asset_ref = profile_context.vdb_asset_ref
        page_text_config_path = profile_context.page_text_config_path
        page_text_config_sha256 = profile_context.page_text_config_sha256
        execution_asset_ref = profile_context.execution_asset_ref
        execution_policy_path = profile_context.execution_policy_path
        execution_policy_sha256 = profile_context.execution_policy_sha256
        web_search_config_path = profile_context.web_search_config_path
        web_search_config_sha256 = profile_context.web_search_config_sha256
        if allowed_tools is not None and tool_operations is not None:
            allowed_names = set(allowed_tools)
            binding_names = set(tool_operations)
            unknown_names = binding_names - allowed_names
            if unknown_names:
                raise MCPConfigurationError(
                    f"{prefix}_TOOL_OPERATIONS_JSON keys must be a subset of "
                    f"{prefix}_ALLOWED_TOOLS_JSON"
                )

        namespace = "web" if kind == MCPServerKind.WEB_API else kind.value
        for tool_name, operations in (tool_operations or {}).items():
            for operation in operations:
                try:
                    canonical_action_key(namespace, operation)
                except ActionNamingError as exc:
                    raise MCPConfigurationError(
                        f"MCP server {server_id!r} tool {tool_name!r}: {exc.reason}: {exc}"
                    ) from exc

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
            tool_resources=_load_tool_resources(prefix, snapshot),
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
            web_search_config_path=web_search_config_path,
            web_search_config_sha256=web_search_config_sha256,
        )

    def request_headers(self) -> dict[str, str]:
        if self.authorization_value is not None:
            return {"Authorization": self.authorization_value}
        if self.bearer_token is None:
            return {}
        return {"Authorization": f"Bearer {self.bearer_token}"}


def mcp_server_ids_from_environment(
    env_var: str = "REDTEAM_MCP_SERVERS", *, source: Mapping[str, str] | None = None
) -> list[str]:
    """Which MCP servers to activate -- the CLI's only source for
    ``capability.mcp_servers`` (there is no application-config file).

    A comma-separated list of server ids, e.g. ``REDTEAM_MCP_SERVERS=recon,web-http``
    -- each id still resolves its own connection details (endpoint/transport/
    credential) via ``MCPServerSettings.from_environment``. Unset or blank
    means no server: the caller gets an honest empty tool catalog, never an
    automatic fallback.
    """
    from redteam.core.environment import mcp_server_list

    return mcp_server_list(source, env_var)


SHELL_POLICY_ENV_VAR = "REDTEAM_SHELL_POLICY_PATH"


def shell_policy_path_from_environment(
    *, source: Mapping[str, str] | None = None
) -> str | None:
    """Operator-owned shell-workroom policy file, or None when disabled.

    A single absolute JSON file path read from REDTEAM_SHELL_POLICY_PATH.
    Unset or blank means the builtin shell provider stays off: callers skip
    the shell assembly and the catalog honestly lacks shell tools, never a
    silent default policy. A set value is returned stripped; the policy
    loader (not this function) validates the file contents.
    """
    from redteam.core.environment import env_snapshot

    raw = env_snapshot(source).get(SHELL_POLICY_ENV_VAR)
    if raw is None:
        return None
    path = raw.strip()
    return path or None


GATED_NETWORK_POLICY_ENV_VAR = "REDTEAM_GATED_NETWORK_POLICY_PATH"


def gated_network_policy_path_from_environment(
    *, source: Mapping[str, str] | None = None
) -> str | None:
    """Operator-owned gated-network policy file, or None when closed.

    A single absolute JSON file path read from
    REDTEAM_GATED_NETWORK_POLICY_PATH. Unset or blank means gated egress
    stays closed: callers pass no gated provider into the composition root
    and the shell keeps its network-closed local-script path, never a
    silent default policy. A set value is returned stripped; the gated
    policy assembly (not this function) validates the file contents.
    """
    from redteam.core.environment import env_snapshot

    raw = env_snapshot(source).get(GATED_NETWORK_POLICY_ENV_VAR)
    if raw is None:
        return None
    path = raw.strip()
    return path or None


def environment_prefix(server_id: str) -> str:
    if not _SAFE_SERVER_ID.fullmatch(server_id):
        raise MCPConfigurationError("MCP server IDs must use lowercase kebab-case")
    return f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"


def _required_environment_value(name: str, env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = source.get(name)
    if value is None or not value.strip():
        raise MCPConfigurationError(f"required environment variable {name} is not set")
    return value.strip()


def _load_bearer_token(prefix: str, env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    token_env_var = source.get(f"{prefix}_TOKEN_ENV_VAR")
    if not token_env_var:
        return None
    if not _SAFE_ENVIRONMENT_NAME.fullmatch(token_env_var):
        raise MCPConfigurationError(
            f"{prefix}_TOKEN_ENV_VAR is not a valid environment variable name"
        )
    token = source.get(token_env_var)
    if not token:
        raise MCPConfigurationError(f"secret environment variable {token_env_var!r} is not set")
    return token


def _load_authorization_value(prefix: str, env: Mapping[str, str] | None = None) -> str | None:
    """Load an exact Authorization value through secret-name indirection.

    Some MCP servers, including ZAP's official MCP add-on, require a raw
    security key rather than a Bearer token.  The value remains outside app
    configuration and descriptor metadata, just like ``TOKEN_ENV_VAR``.
    """

    source = os.environ if env is None else env
    value_env_var = source.get(f"{prefix}_AUTHORIZATION_ENV_VAR")
    if not value_env_var:
        return None
    if not _SAFE_ENVIRONMENT_NAME.fullmatch(value_env_var):
        raise MCPConfigurationError(
            f"{prefix}_AUTHORIZATION_ENV_VAR is not a valid environment variable name"
        )
    value = source.get(value_env_var)
    if not value:
        raise MCPConfigurationError(f"secret environment variable {value_env_var!r} is not set")
    if "\r" in value or "\n" in value:
        raise MCPConfigurationError("Authorization value must not contain line breaks")
    return value


def _load_stdio_arguments(prefix: str, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if env is None else env
    raw_arguments = source.get(f"{prefix}_ARGS_JSON", "[]")
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


def _load_allowed_tools(
    prefix: str, env: Mapping[str, str] | None = None
) -> tuple[str, ...] | None:
    source = os.environ if env is None else env
    value = source.get(f"{prefix}_ALLOWED_TOOLS_JSON")
    if value is None:
        return None
    try:
        names = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError("ALLOWED_TOOLS_JSON must be a JSON array") from exc
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(names) != len(set(names))
    ):
        raise MCPConfigurationError("ALLOWED_TOOLS_JSON must contain unique non-empty tool names")
    return tuple(names)


def _load_tool_operations(
    prefix: str, env: Mapping[str, str] | None = None
) -> dict[str, tuple[str, ...]] | None:
    source = os.environ if env is None else env
    value = source.get(f"{prefix}_TOOL_OPERATIONS_JSON")
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
            or any(not isinstance(operation, str) for operation in operations)
            or len(operations) != len(set(operations))
        ):
            raise MCPConfigurationError(
                "TOOL_OPERATIONS_JSON values must be arrays of unique lower_snake_case operations"
            )
        for operation in operations:
            try:
                validate_operation_format(operation)
            except ActionNamingError as exc:
                raise MCPConfigurationError(
                    f"{prefix}_TOOL_OPERATIONS_JSON tool {tool_name!r}: {exc.reason}: {exc}"
                ) from exc
        bindings[tool_name] = tuple(operations)
    return bindings


def _load_tool_resources(
    prefix: str, env: Mapping[str, str]
) -> dict[str, list[ToolResourceBinding]]:
    value = env.get(prefix + "_TOOL_RESOURCES_JSON")
    if value is None:
        return {}
    try:
        bindings = TypeAdapter(dict[str, list[ToolResourceBinding]]).validate_json(value)
        if any(not name.strip() or len(rows) > 32 for name, rows in bindings.items()):
            raise ValueError("invalid resource declarations")
        return bindings
    except (ValidationError, ValueError):
        raise MCPConfigurationError("invalid TOOL_RESOURCES_JSON") from None


def _load_max_concurrency(prefix: str, env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    value = source.get(f"{prefix}_MAX_CONCURRENCY", "1")
    try:
        limit = int(value)
    except ValueError as exc:
        raise MCPConfigurationError(f"{prefix}_MAX_CONCURRENCY must be an integer") from exc
    if not 1 <= limit <= 32:
        raise MCPConfigurationError(f"{prefix}_MAX_CONCURRENCY must be between 1 and 32")
    return limit


def _load_max_output_bytes(prefix: str, env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    value = source.get(f"{prefix}_MAX_OUTPUT_BYTES", "1048576")
    try:
        limit = int(value)
    except ValueError as exc:
        raise MCPConfigurationError(f"{prefix}_MAX_OUTPUT_BYTES must be an integer") from exc
    if not 1024 <= limit <= 16 * 1024 * 1024:
        raise MCPConfigurationError(f"{prefix}_MAX_OUTPUT_BYTES must be between 1024 and 16777216")
    return limit


def _load_process_environment(prefix: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    raw_names = source.get(f"{prefix}_PASSTHROUGH_ENV", "")
    names = [name.strip() for name in raw_names.split(",") if name.strip()]
    environment: dict[str, str] = {}
    for name in names:
        if not _SAFE_ENVIRONMENT_NAME.fullmatch(name):
            raise MCPConfigurationError(
                f"{prefix}_PASSTHROUGH_ENV contains an invalid environment variable name"
            )
        value = source.get(name)
        if value is None:
            raise MCPConfigurationError(f"stdio environment variable {name!r} is not set")
        environment[name] = value
    return environment


def _validate_command(command: str, prefix: str) -> None:
    if "\x00" in command or "\n" in command or "\r" in command:
        raise MCPConfigurationError(f"{prefix}_COMMAND must be a single executable name or path")


def _validate_endpoint(endpoint: str, prefix: str, env: Mapping[str, str] | None = None) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MCPConfigurationError(f"{prefix}_ENDPOINT must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MCPConfigurationError(
            f"{prefix}_ENDPOINT must not contain credentials; use TOKEN_ENV_VAR "
            f"or AUTHORIZATION_ENV_VAR"
        )
    allow_insecure = (
        (os.environ if env is None else env).get(f"{prefix}_ALLOW_INSECURE_HTTP", "false").lower()
    )
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS and allow_insecure != "true":
        raise MCPConfigurationError(
            f"{prefix}_ENDPOINT must use HTTPS outside local development; "
            f"set {prefix}_ALLOW_INSECURE_HTTP=true only for an authorized lab"
        )
