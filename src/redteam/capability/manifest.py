"""Validation for the metadata contract exposed by discovered MCP tools.

Discovery is an untrusted boundary: a remote server supplies both JSON
schemas and names that Agent/Orchestration will later use to build action
candidates.  This module keeps that boundary explicit and fail-closed.  It
does not execute a tool or decide whether an action is allowed.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from redteam.capability.settings import MCPConfigurationError
from redteam.core.action_semantics import CANONICAL_CAPABILITIES, CANONICAL_OPERATIONS
from redteam.core.contracts.tool import ToolDescriptor

_CANONICAL_OPERATION = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

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
    _validate_operations(descriptor.operations, prefix)
    _validate_target_types(descriptor.target_types, prefix)


def _validate_identity(descriptor: ToolDescriptor, prefix: str) -> None:
    tool_id = str(descriptor.tool_id)
    namespace, separator, _remote_name = tool_id.partition(".")
    if not separator or not namespace or not _remote_name or "." in _remote_name:
        raise MCPConfigurationError(
            f"{prefix} must use <namespace>.<remote_tool_name> tool_id format"
        )
    if not descriptor.server_id.strip() or not descriptor.tool_name.strip():
        raise MCPConfigurationError(f"{prefix} must have non-empty server_id and tool_name")
    if not descriptor.capability_tags:
        raise MCPConfigurationError(f"{prefix} must advertise at least one capability tag")
    if namespace not in descriptor.capability_tags:
        raise MCPConfigurationError(
            f"{prefix} namespace {namespace!r} is missing from capability_tags"
        )
    if namespace not in CANONICAL_CAPABILITIES:
        raise MCPConfigurationError(
            f"{prefix} namespace {namespace!r} is not a canonical capability"
        )


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
        not isinstance(operation, str) or not _CANONICAL_OPERATION.fullmatch(operation)
        for operation in operations
    ):
        raise MCPConfigurationError(
            f"{prefix} operations must be unique lower_snake_case values"
        )
    unknown = [operation for operation in operations if operation not in CANONICAL_OPERATIONS]
    if unknown:
        raise MCPConfigurationError(
            f"{prefix} operations are not canonical actions: {', '.join(unknown)}"
        )


def _validate_target_types(target_types: list[str], prefix: str) -> None:
    if len(target_types) != len(set(target_types)) or any(
        target_type not in KNOWN_TARGET_TYPES for target_type in target_types
    ):
        raise MCPConfigurationError(
            f"{prefix} target_types contain an unknown or duplicate target kind"
        )
