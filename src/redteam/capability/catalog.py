"""Capability-owned catalog for resolving canonical actions to MCP tools."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field

from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId

_CAPABILITY_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]*$")
_CANONICAL_OPERATION = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class ToolCandidateMetadata(BaseModel):
    """Capability-owned discovery view for one tool and canonical operation.

    Unknown schema details remain ``None`` rather than being inferred. A Core
    Port change is required before Orchestration can consume this view.
    """

    tool_id: ToolId
    capability: str
    operation: str
    required_arguments: tuple[str, ...] | None
    optional_arguments: tuple[str, ...] | None
    target_type: str | None
    risk_level: RiskLevel
    output_schema: dict[str, Any] | None
    server_id: str
    tool_name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    health_status: ToolHealthStatus


class MCPToolCatalog:
    """Read-only action-to-tool view over one Registry discovery snapshot.

    It returns candidates; it never chooses one. Selection policy remains with
    Orchestration after Control has returned ALLOW.
    """

    def __init__(
        self,
        registry: MCPToolRegistry,
        operation_bindings: Mapping[str, Collection[str]] | None = None,
        *,
        output_schemas: Mapping[str, dict[str, Any] | None] | None = None,
        target_types: Mapping[str, str | None] | None = None,
    ) -> None:
        self._registry = registry
        self._operation_bindings = (
            {
                ToolId(str(tool_id)): frozenset(operations)
                for tool_id, operations in operation_bindings.items()
            }
            if operation_bindings is not None
            else None
        )
        if self._operation_bindings is not None:
            invalid_operations = sorted(
                operation
                for operations in self._operation_bindings.values()
                for operation in operations
                if not _CANONICAL_OPERATION.fullmatch(operation)
            )
            if invalid_operations:
                names = ", ".join(invalid_operations)
                raise ValueError(f"operation bindings contain noncanonical names: {names}")
            registered = {descriptor.tool_id for descriptor in registry.all()}
            unknown = set(self._operation_bindings) - registered
            if unknown:
                names = ", ".join(sorted(str(tool_id) for tool_id in unknown))
                raise ValueError(f"operation bindings reference unknown tools: {names}")
        registered = {descriptor.tool_id for descriptor in registry.all()}
        self._output_schemas = {
            ToolId(str(tool_id)): deepcopy(schema)
            for tool_id, schema in (output_schemas or {}).items()
        }
        self._target_types = {
            ToolId(str(tool_id)): target_type
            for tool_id, target_type in (target_types or {}).items()
        }
        unknown_metadata = (set(self._output_schemas) | set(self._target_types)) - registered
        if unknown_metadata:
            names = ", ".join(sorted(str(tool_id) for tool_id in unknown_metadata))
            raise ValueError(f"candidate metadata references unknown tools: {names}")

    def supports(self, tool_id: ToolId, operation: str) -> bool:
        descriptor = self._registry.get(tool_id)
        if descriptor is None:
            return False
        return operation in self._operations_for(descriptor)

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        return self._registry.get(tool_id)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return self._registry.list_by_capability(capability_tag)

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolDescriptor]:
        """Return deterministic eligible candidates without selecting a tool."""

        if not _CAPABILITY_NAMESPACE.fullmatch(capability):
            raise ValueError("capability must be a lowercase namespace")
        if not _CANONICAL_OPERATION.fullmatch(operation):
            raise ValueError("operation must use lower_snake_case")

        candidates: list[ToolDescriptor] = []
        for descriptor in self._registry.all():
            namespace, separator, _ = str(descriptor.tool_id).partition(".")
            if separator != "." or namespace != capability:
                continue
            if operation not in self._operations_for(descriptor):
                continue
            if (
                not include_unhealthy
                and descriptor.health_status == ToolHealthStatus.UNHEALTHY
            ):
                continue
            candidates.append(descriptor)
        return candidates

    def list_candidate_metadata(
        self,
        capability: str | None = None,
        operation: str | None = None,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolCandidateMetadata]:
        """Project discovered tools into candidate records without choosing one."""

        if capability is not None and not _CAPABILITY_NAMESPACE.fullmatch(capability):
            raise ValueError("capability must be a lowercase namespace")
        if operation is not None and not _CANONICAL_OPERATION.fullmatch(operation):
            raise ValueError("operation must use lower_snake_case")

        candidates: list[ToolCandidateMetadata] = []
        for descriptor in self._registry.all():
            namespace, separator, _ = str(descriptor.tool_id).partition(".")
            if separator != "." or (capability is not None and namespace != capability):
                continue
            if (
                not include_unhealthy
                and descriptor.health_status == ToolHealthStatus.UNHEALTHY
            ):
                continue
            required, optional = _argument_names(descriptor.input_schema)
            for bound_operation in sorted(self._operations_for(descriptor)):
                if operation is not None and bound_operation != operation:
                    continue
                candidates.append(
                    ToolCandidateMetadata(
                        tool_id=descriptor.tool_id,
                        capability=namespace,
                        operation=bound_operation,
                        required_arguments=required,
                        optional_arguments=optional,
                        target_type=self._target_types.get(descriptor.tool_id),
                        risk_level=descriptor.risk_level,
                        output_schema=deepcopy(
                            self._output_schemas.get(descriptor.tool_id)
                        ),
                        server_id=descriptor.server_id,
                        tool_name=descriptor.tool_name,
                        description=descriptor.description,
                        input_schema=deepcopy(descriptor.input_schema),
                        health_status=descriptor.health_status,
                    )
                )
        return candidates

    def _operations_for(self, descriptor: ToolDescriptor) -> frozenset[str]:
        if self._operation_bindings is None:
            return frozenset((*descriptor.operations, descriptor.tool_name))
        return self._operation_bindings.get(descriptor.tool_id, frozenset())


def _argument_names(
    input_schema: dict[str, Any],
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    """Read top-level MCP arguments; do not claim an absent schema has no args."""

    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return None, None
    required = input_schema.get("required", [])
    if (
        not isinstance(required, list)
        or any(not isinstance(name, str) for name in required)
        or any(not isinstance(name, str) for name in properties)
        or not set(required).issubset(properties)
    ):
        raise ValueError("MCP input schema has invalid required/properties fields")
    return tuple(sorted(set(required))), tuple(sorted(set(properties) - set(required)))
