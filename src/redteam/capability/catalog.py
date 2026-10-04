"""Capability-owned catalog for resolving canonical actions to MCP tools."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from redteam.capability.adapters.mcp.naming import (
    is_canonical_mcp_tool_id,
    parse_canonical_mcp_tool_id,
)
from redteam.capability.registry import MCPToolRegistry
from redteam.core.action_semantics import (
    ActionNamingError,
    canonical_action_key,
    validate_capability_name,
    validate_operation_name,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId

_MetadataT = TypeVar("_MetadataT")


def _resolve_metadata_keys(
    registry: MCPToolRegistry,
    values: Mapping[str, _MetadataT],
    label: str,
) -> dict[ToolId, _MetadataT]:
    resolved: dict[ToolId, _MetadataT] = {}
    for key, value in values.items():
        descriptor = registry.resolve(ToolId(str(key)))
        if descriptor is None:
            raise ValueError(f"{label} reference unknown tools: {key}")
        tool_id = descriptor.tool_id
        if tool_id in resolved and resolved[tool_id] != value:
            raise ValueError(f"{label} conflict for the same tool: {tool_id}")
        resolved[tool_id] = value
    return resolved


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


def _namespace_and_remote(tool_id: ToolId) -> tuple[str, str] | None:
    """Namespace plus remote name for 2-part or canonical 3-part ids."""

    text = str(tool_id)
    if is_canonical_mcp_tool_id(text):
        namespace, _server_id, remote = parse_canonical_mcp_tool_id(text)
        return namespace, remote
    namespace, separator, remote = text.partition(".")
    if separator != "." or not remote or "." in remote:
        return None
    return namespace, remote


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
            _resolve_metadata_keys(
                registry,
                {
                    str(tool_id): frozenset(operations)
                    for tool_id, operations in operation_bindings.items()
                },
                "operation bindings",
            )
            if operation_bindings is not None
            else None
        )
        registered = {descriptor.tool_id for descriptor in registry.all()}
        self._resolved_operations: dict[ToolId, frozenset[str]] = {}
        if self._operation_bindings is not None:
            for tool_id in registered:
                descriptor = registry.get(tool_id)
                if descriptor is None:
                    continue
                parsed = _namespace_and_remote(descriptor.tool_id)
                if parsed is None:
                    raise ValueError(f"operation bindings reference malformed tool id: {tool_id}")
                namespace, _remote = parsed
                try:
                    validate_capability_name(namespace)
                except ActionNamingError as exc:
                    raise ValueError(
                        f"tool {tool_id} uses unsupported namespace {namespace!r}"
                    ) from exc
                if tool_id not in self._operation_bindings:
                    self._resolved_operations[tool_id] = frozenset()
                    continue
                declared = list(self._operation_bindings[tool_id])
                if descriptor.operations and set(declared) != set(descriptor.operations):
                    raise ValueError(
                        f"operation bindings conflict with declared operations: {tool_id}"
                    )
                resolved = set()
                for operation in declared:
                    try:
                        resolved.add(canonical_action_key(namespace, operation).split(".", 1)[1])
                    except ActionNamingError as exc:
                        raise ValueError(
                            f"operation bindings contain noncanonical names: {exc.reason}"
                        ) from exc
                self._resolved_operations[tool_id] = frozenset(resolved)
        else:
            for tool_id in registered:
                descriptor = registry.get(tool_id)
                if descriptor is None:
                    continue
                parsed = _namespace_and_remote(descriptor.tool_id)
                if parsed is None:
                    raise ValueError(f"registry holds malformed tool id: {tool_id}")
                namespace = parsed[0]
                try:
                    validate_capability_name(namespace)
                except ActionNamingError as exc:
                    raise ValueError(
                        f"tool {tool_id} uses unsupported namespace {namespace!r}"
                    ) from exc
                candidates = list(descriptor.operations)
                resolved = set()
                for operation in candidates:
                    try:
                        resolved.add(canonical_action_key(namespace, operation).split(".", 1)[1])
                    except ActionNamingError as exc:
                        raise ValueError(
                            f"tool {tool_id} has noncanonical names: {exc.reason}"
                        ) from exc
                self._resolved_operations[tool_id] = frozenset(resolved)
        self._output_schemas = _resolve_metadata_keys(
            registry,
            {str(key): deepcopy(value) for key, value in (output_schemas or {}).items()},
            "candidate metadata",
        )
        self._target_types = _resolve_metadata_keys(
            registry,
            {str(key): value for key, value in (target_types or {}).items()},
            "candidate metadata",
        )

    def supports(self, tool_id: ToolId, operation: str) -> bool:
        descriptor = self._registry.resolve(tool_id)
        if descriptor is None:
            return False
        if not operation:
            return True
        return operation in self._operations_for(descriptor)

    def select(self, tool_ids) -> list:
        chosen = []
        for tool_id in tool_ids:
            descriptor = self.get(ToolId(str(tool_id)))
            if descriptor is not None:
                chosen.append(descriptor)
        return chosen

    def all_descriptors(self) -> list:
        """Every registered tool as a finalized descriptor, sorted by id.

        Role views filter this list without consulting operation
        bindings, so mapper-less tools stay visible and ops-empty
        tools are still listed.
        """
        return [
            self.finalized(descriptor)
            for descriptor in sorted(
                self._registry.all(), key=lambda item: str(item.tool_id)
            )
        ]

    def executable_pairs(self) -> list[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for tool_id, operations in self._resolved_operations.items():
            parsed = _namespace_and_remote(tool_id)
            if parsed is None:
                continue
            for operation in operations:
                pairs.add((parsed[0], operation))
        return sorted(pairs)

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        descriptor = self._registry.resolve(tool_id)
        if descriptor is None:
            return None
        return self.finalized(descriptor)

    def resolve(self, tool_id: ToolId) -> ToolDescriptor | None:
        """Canonical-first single resolver over the finalized catalog view."""

        stored = self._registry.resolve(ToolId(str(tool_id)))
        if stored is None:
            return None
        return self.finalized(stored)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return [self.finalized(item) for item in self._registry.list_by_capability(capability_tag)]

    def finalized(self, descriptor: ToolDescriptor) -> ToolDescriptor:
        """Copy of ``descriptor`` carrying the resolved canonical operations.

        Individual lookups show the same finalized operation list as the
        action queries; a tool whose binding cannot be resolved is shown
        with no operations rather than as a registered operation.
        """
        return descriptor.model_copy(
            update={
                "operations": sorted(self._operations_for(descriptor)),
                "output_schema": deepcopy(
                    self._output_schemas.get(descriptor.tool_id, descriptor.output_schema)
                ),
                "target_types": (
                    [self._target_types[descriptor.tool_id]]
                    if self._target_types.get(descriptor.tool_id) is not None
                    else []
                    if descriptor.tool_id in self._target_types
                    else list(descriptor.target_types)
                ),
            }
        )

    def health_snapshot(self) -> dict[str, ToolHealthStatus]:
        """Read-only per-server health view straight from the live Registry.

        This never probes a server, never mutates state, and never decides
        policy. It is the synchronous companion to
        ``MCPToolGateway.refresh_health``: refresh first, then read this.
        """

        return server_health_snapshot(self._registry)

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolDescriptor]:
        """Return deterministic eligible candidates without selecting a tool."""

        canonical_action_key(capability, operation)

        candidates: list[ToolDescriptor] = []
        for descriptor in self._registry.all():
            parsed = _namespace_and_remote(descriptor.tool_id)
            if parsed is None or parsed[0] != capability:
                continue
            if operation not in self._operations_for(descriptor):
                continue
            if not include_unhealthy and descriptor.health_status == ToolHealthStatus.UNHEALTHY:
                continue
            candidates.append(self.finalized(descriptor))
        return candidates

    def list_candidate_metadata(
        self,
        capability: str | None = None,
        operation: str | None = None,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolCandidateMetadata]:
        """Project discovered tools into candidate records without choosing one."""

        if capability is not None:
            validate_capability_name(capability)
        if operation is not None:
            validate_operation_name(operation)

        candidates: list[ToolCandidateMetadata] = []
        for descriptor in self._registry.all():
            descriptor = self.finalized(descriptor)
            parsed = _namespace_and_remote(descriptor.tool_id)
            if parsed is None or (capability is not None and parsed[0] != capability):
                continue
            namespace = parsed[0]
            if not include_unhealthy and descriptor.health_status == ToolHealthStatus.UNHEALTHY:
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
                        target_type=(
                            descriptor.target_types[0]
                            if len(descriptor.target_types) == 1
                            else None
                        ),
                        risk_level=descriptor.risk_level,
                        output_schema=deepcopy(descriptor.output_schema),
                        server_id=descriptor.server_id,
                        tool_name=descriptor.tool_name,
                        description=descriptor.description,
                        input_schema=deepcopy(descriptor.input_schema),
                        health_status=descriptor.health_status,
                    )
                )
        return candidates

    def _operations_for(self, descriptor: ToolDescriptor) -> frozenset[str]:
        return self._resolved_operations.get(descriptor.tool_id, frozenset())


def server_health_snapshot(registry: MCPToolRegistry) -> dict[str, ToolHealthStatus]:
    """Aggregate a Registry snapshot into one status per MCP server.

    Descriptors belonging to one server are refreshed in unison, so mixed
    results normally cannot happen; if they ever do, ``UNHEALTHY`` wins so the
    view errs toward caution.
    """

    snapshot: dict[str, ToolHealthStatus] = {}
    for descriptor in registry.all():
        current = snapshot.get(descriptor.server_id)
        if current is None or descriptor.health_status == ToolHealthStatus.UNHEALTHY:
            snapshot[descriptor.server_id] = descriptor.health_status
    return snapshot


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
    return tuple(required), tuple(name for name in properties if name not in required)
