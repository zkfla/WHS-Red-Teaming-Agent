"""Validation for the metadata contract exposed by discovered MCP tools.

Discovery is an untrusted boundary: a remote server supplies both JSON
schemas and names that Agent/Orchestration will later use to build action
candidates.  This module keeps that boundary explicit and fail-closed.  It
does not execute a tool or decide whether an action is allowed.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from redteam.capability.adapters.mcp.naming import parse_canonical_mcp_tool_id
from redteam.capability.settings import MCPConfigurationError
from redteam.core.action_semantics import (
    ActionNamingError,
    canonical_action_key,
    validate_capability_name,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import ToolId
from redteam.core.provider_contracts import TARGET_ARGUMENT_VALUES

# These are graph/target kinds understood by the shared Core contracts.  A
# server may advertise fewer kinds, but an unknown kind would be invisible to
# the Agent target binder and therefore must not enter the Registry.
KNOWN_TARGET_TYPES = frozenset(
    {
        "asset",
        "device",
        "domain",
        "endpoint",
        "graph_node",
        "host",
        "observation",
        "principal",
        "service",
        "url",
    }
)


def split_tool_id(value: ToolId | str) -> tuple[str, str | None, str] | None:
    """Split a legacy 2-part or canonical 3-part tool id."""

    parts = str(value).split(".")
    if len(parts) == 2 and all(parts):
        return (parts[0], None, parts[1])
    if len(parts) == 3 and all(parts):
        return (parts[0], parts[1], parts[2])
    return None


def validate_tool_manifest(descriptor: ToolDescriptor) -> None:
    """Validate one discovered descriptor before it enters the Registry.

    ``ToolDescriptor.input_schema`` and ``output_schema`` remain the source
    of truth.  The convenience metadata (operations and target types) is
    checked here so the manifest shown to Agent is the same contract that the
    remote MCP discovery response actually advertised.
    """

    tool_name = descriptor.tool_name or str(descriptor.tool_id)
    prefix = f"MCP tool {tool_name!r}"
    _validate_identity(descriptor, prefix)
    _validate_schema(descriptor.input_schema, f"{prefix} input schema")
    if descriptor.output_schema is not None:
        _validate_schema(descriptor.output_schema, f"{prefix} output schema")
    _validate_required_arguments(descriptor.input_schema, prefix)
    properties = descriptor.input_schema.get("properties", {})
    if any(
        name not in properties or value not in TARGET_ARGUMENT_VALUES
        for name, value in descriptor.argument_bindings.items()
    ):
        raise MCPConfigurationError(f"{prefix} has an invalid target argument binding")
    _validate_operations(descriptor.operations, prefix)
    namespace = str(descriptor.tool_id).partition(".")[0]
    for operation in descriptor.operations:
        try:
            canonical_action_key(namespace, operation)
        except ActionNamingError as exc:
            raise MCPConfigurationError(
                f"{prefix} (server {descriptor.server_id!r}): {exc.reason}: {exc}"
            ) from exc
    _validate_target_types(descriptor.target_types, prefix)


def _validate_identity(descriptor: ToolDescriptor, prefix: str) -> None:
    tool_id = str(descriptor.tool_id)
    split = split_tool_id(tool_id)
    if split is None:
        raise MCPConfigurationError(
            f"{prefix} must use <namespace>.<remote_tool_name> tool_id format"
        )
    namespace, server_id, _remote_name = split
    if server_id is not None:
        try:
            namespace, physical_server, remote_name = parse_canonical_mcp_tool_id(tool_id)
        except ValueError as exc:
            raise MCPConfigurationError(
                f"{prefix} must use <namespace>.<remote_tool_name> tool_id format"
            ) from exc
        if (physical_server, remote_name) != (descriptor.server_id, descriptor.tool_name):
            raise MCPConfigurationError(f"{prefix} tool identity differs from its physical server")
    if not descriptor.server_id.strip() or not descriptor.tool_name.strip():
        raise MCPConfigurationError(f"{prefix} must have non-empty server_id and tool_name")
    if not descriptor.capability_tags:
        raise MCPConfigurationError(f"{prefix} must advertise at least one capability tag")
    if namespace not in descriptor.capability_tags:
        raise MCPConfigurationError(
            f"{prefix} namespace {namespace!r} is missing from capability_tags"
        )
    try:
        validate_capability_name(namespace)
    except ActionNamingError as exc:
        raise MCPConfigurationError(
            f"{prefix} namespace {namespace!r} is not a canonical capability"
        ) from exc


def _validate_schema(schema: dict[str, Any], label: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise MCPConfigurationError(f"{label} is not a valid JSON Schema") from exc


def _validate_required_arguments(schema: dict[str, Any], prefix: str) -> None:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise MCPConfigurationError(f"{prefix} required arguments must be a list of strings")
    if not required:
        return
    if not isinstance(properties, dict):
        raise MCPConfigurationError(
            f"{prefix} required arguments cannot be resolved because properties is missing"
        )
    missing = sorted(set(required) - set(properties))
    if missing:
        raise MCPConfigurationError(
            f"{prefix} required arguments are absent from properties: {', '.join(missing)}"
        )


def _validate_operations(operations: list[str], prefix: str) -> None:
    if len(operations) != len(set(operations)) or any(
        not isinstance(operation, str) for operation in operations
    ):
        raise MCPConfigurationError(f"{prefix} operations must be unique lower_snake_case values")


def _validate_target_types(target_types: list[str], prefix: str) -> None:
    if len(target_types) != len(set(target_types)) or any(
        target_type not in KNOWN_TARGET_TYPES for target_type in target_types
    ):
        raise MCPConfigurationError(
            f"{prefix} target_types contain an unknown or duplicate target kind"
        )
