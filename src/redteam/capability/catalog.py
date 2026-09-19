"""Capability-owned catalog for resolving canonical actions to MCP tools."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId

_CAPABILITY_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]*$")
_CANONICAL_OPERATION = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class MCPToolCatalog:
    """Read-only action-to-tool view over one Registry discovery snapshot.

    It returns candidates; it never chooses one. Selection policy remains with
    Orchestration after Control has returned ALLOW.
    """

    def __init__(
        self,
        registry: MCPToolRegistry,
        operation_bindings: Mapping[str, Collection[str]] | None = None,
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

    def supports(self, tool_id: ToolId, operation: str) -> bool:
        descriptor = self._registry.get(tool_id)
        if descriptor is None:
            return False
        return operation in self._operations_for(descriptor)

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

    def _operations_for(self, descriptor: ToolDescriptor) -> frozenset[str]:
        if self._operation_bindings is None:
            return frozenset((descriptor.tool_name,))
        return self._operation_bindings.get(descriptor.tool_id, frozenset())
