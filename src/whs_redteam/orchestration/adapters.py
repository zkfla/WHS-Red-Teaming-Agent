"""Allowlisted adapters between the dispatcher and local or MCP tools."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import (
    ProbeRegistrationError,
    ToolProtocolError,
    ToolTransportUnavailable,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    Capability,
    CleanupTask,
    ToolBackend,
    ToolCall,
    ToolExecutionResult,
)
from whs_redteam.policy.capability_catalog import CAPABILITY_CATALOG


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Restricted local services; raw HTTP, browser, and shell are intentionally absent."""

    run_id: str
    inventory: InventoryStore
    audit_logger: AuditLogger
    config: EngagementConfig | None = None


class Probe(Protocol):
    capability: Capability

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, Any]: ...


class ProbeRegistry:
    def __init__(self) -> None:
        self._probes: dict[Capability, Probe] = {}
        self._cleanup_handlers: dict[
            Capability,
            Callable[[dict[str, Any], ProbeContext], Awaitable[dict[str, Any]]],
        ] = {}

    def register(self, probe: Probe) -> None:
        if probe.capability in self._probes:
            raise ProbeRegistrationError(
                f"probe is already registered: {probe.capability.value}"
            )
        self._probes[probe.capability] = probe

    def resolve(self, capability: Capability) -> Probe:
        try:
            return self._probes[capability]
        except KeyError as exc:
            raise ProbeRegistrationError(
                f"no probe registered for capability: {capability.value}"
            ) from exc

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(self._probes)

    def register_cleanup(
        self,
        capability: Capability,
        handler: Callable[
            [dict[str, Any], ProbeContext], Awaitable[dict[str, Any]]
        ],
    ) -> None:
        if capability in self._cleanup_handlers:
            raise ProbeRegistrationError(
                f"cleanup handler is already registered: {capability.value}"
            )
        self._cleanup_handlers[capability] = handler

    def resolve_cleanup(
        self, capability: Capability
    ) -> Callable[[dict[str, Any], ProbeContext], Awaitable[dict[str, Any]]]:
        try:
            return self._cleanup_handlers[capability]
        except KeyError as exc:
            raise ProbeRegistrationError(
                f"no cleanup handler registered for capability: {capability.value}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    capability: Capability
    backend: ToolBackend
    tool_name: str
    cleanup_tool_name: str | None = None


class ToolAdapter(Protocol):
    """A dispatcher-only execution boundary."""

    def resolve(self, capability: Capability) -> ToolDescriptor: ...

    async def execute(
        self, call: ToolCall, context: ProbeContext
    ) -> ToolExecutionResult: ...

    async def execute_cleanup(
        self, task: CleanupTask, context: ProbeContext
    ) -> dict[str, Any]: ...


class LocalProbeAdapter:
    """Runs the existing probes behind the same contract used by MCP."""

    def __init__(self, registry: ProbeRegistry) -> None:
        self.registry = registry

    def resolve(self, capability: Capability) -> ToolDescriptor:
        self.registry.resolve(capability)
        cleanup_tool_name = None
        if CAPABILITY_CATALOG[capability].cleanup_required:
            self.registry.resolve_cleanup(capability)
            cleanup_tool_name = f"local.{capability.value}.cleanup"
        return ToolDescriptor(
            capability=capability,
            backend=ToolBackend.LOCAL,
            tool_name=f"local.{capability.value}",
            cleanup_tool_name=cleanup_tool_name,
        )

    async def execute(
        self, call: ToolCall, context: ProbeContext
    ) -> ToolExecutionResult:
        if call.run_id != context.run_id:
            raise ToolProtocolError("tool call run_id does not match its context")
        probe = self.registry.resolve(call.capability)
        output = await probe.execute(call.to_proposal(), context)
        if not isinstance(output, dict):
            raise ToolProtocolError("local probe output must be a dictionary")
        output = dict(output)
        cleanup_arguments = output.pop("_cleanup_arguments", None)
        spec = CAPABILITY_CATALOG[call.capability]
        if spec.cleanup_required and not isinstance(cleanup_arguments, Mapping):
            raise ToolProtocolError(
                "cleanup-required probe must return _cleanup_arguments"
            )
        if not spec.cleanup_required and cleanup_arguments is not None:
            raise ToolProtocolError(
                "probe returned cleanup arguments for a read-only capability"
            )
        return ToolExecutionResult(
            call_id=call.call_id,
            backend=ToolBackend.LOCAL,
            tool_name=f"local.{call.capability.value}",
            output=output,
            cleanup_tool_name=(
                f"local.{call.capability.value}.cleanup"
                if spec.cleanup_required
                else None
            ),
            cleanup_arguments=(
                dict(cleanup_arguments)
                if isinstance(cleanup_arguments, Mapping)
                else None
            ),
        )

    async def execute_cleanup(
        self, task: CleanupTask, context: ProbeContext
    ) -> dict[str, Any]:
        expected = self.resolve(task.capability).cleanup_tool_name
        if expected is None or task.tool_name != expected:
            raise ToolProtocolError("cleanup task does not match its local binding")
        handler = self.registry.resolve_cleanup(task.capability)
        output = await handler(task.arguments, context)
        if not isinstance(output, dict):
            raise ToolProtocolError("cleanup handler output must be a dictionary")
        return output


class MCPTransport(Protocol):
    """Small transport seam so the MCP SDK is an optional runtime dependency."""

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MCPToolBinding:
    capability: Capability
    server_name: str
    tool_name: str
    cleanup_tool_name: str | None = None


class MCPProbeAdapter:
    """Capability-to-tool allowlist. It never accepts planner-supplied tool names."""

    def __init__(
        self,
        bindings: tuple[MCPToolBinding, ...],
        transport: MCPTransport | None,
        *,
        max_response_bytes: int = 1_000_000,
        authorization_receipt_factory: Callable[[ToolCall], str] | None = None,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.transport = transport
        self.max_response_bytes = max_response_bytes
        self.authorization_receipt_factory = authorization_receipt_factory
        self._bindings: dict[Capability, MCPToolBinding] = {}
        for binding in bindings:
            if binding.capability in self._bindings:
                raise ProbeRegistrationError(
                    f"MCP capability is already bound: {binding.capability.value}"
                )
            if not binding.server_name or not binding.tool_name:
                raise ValueError("MCP server and tool names must not be empty")
            self._bindings[binding.capability] = binding

    def resolve(self, capability: Capability) -> ToolDescriptor:
        try:
            binding = self._bindings[capability]
        except KeyError as exc:
            raise ProbeRegistrationError(
                f"no MCP tool registered for capability: {capability.value}"
            ) from exc
        if (
            CAPABILITY_CATALOG[capability].cleanup_required
            and binding.cleanup_tool_name is None
        ):
            raise ProbeRegistrationError(
                f"no MCP cleanup tool registered: {capability.value}"
            )
        if (
            CAPABILITY_CATALOG[capability].approval_required
            and self.authorization_receipt_factory is None
        ):
            raise ToolProtocolError(
                "sensitive MCP capability requires a signed server-side receipt"
            )
        return ToolDescriptor(
            capability=capability,
            backend=ToolBackend.MCP,
            tool_name=f"{binding.server_name}.{binding.tool_name}",
            cleanup_tool_name=(
                f"{binding.server_name}.{binding.cleanup_tool_name}"
                if binding.cleanup_tool_name
                else None
            ),
        )

    async def execute(
        self, call: ToolCall, context: ProbeContext
    ) -> ToolExecutionResult:
        if call.run_id != context.run_id:
            raise ToolProtocolError("tool call run_id does not match its context")
        descriptor = self.resolve(call.capability)
        binding = self._bindings[call.capability]
        if self.transport is None:
            raise ToolTransportUnavailable("MCP transport is not configured")
        request = {
            "call_id": call.call_id,
            "action_id": call.action_id,
            "queue_id": call.queue_id,
            "run_id": call.run_id,
            "engagement_name": call.engagement_name,
            "capability": call.capability.value,
            "target_url": call.target_url,
            "method": call.method,
            "risk_level": call.risk_level.value,
            "actor_id": call.actor_id,
            "fixture_id": call.fixture_id,
            "approval_verified": call.approval_verified,
        }
        if CAPABILITY_CATALOG[call.capability].approval_required:
            if not call.approval_verified:
                raise ToolProtocolError(
                    "sensitive MCP call lacks verified central approval"
                )
            if self.authorization_receipt_factory is None:
                raise ToolProtocolError(
                    "sensitive MCP call lacks a server authorization receipt"
                )
            receipt = self.authorization_receipt_factory(call)
            if not receipt:
                raise ToolProtocolError("server authorization receipt is empty")
            request["authorization_receipt"] = receipt
        response = await self.transport.call_tool(
            binding.server_name, binding.tool_name, {"request": request}
        )
        if not isinstance(response, Mapping):
            raise ToolProtocolError("MCP tool response must be an object")
        try:
            encoded = json.dumps(response, ensure_ascii=False).encode()
        except (TypeError, ValueError) as exc:
            raise ToolProtocolError("MCP tool response is not JSON serializable") from exc
        if len(encoded) > self.max_response_bytes:
            raise ToolProtocolError("MCP tool response exceeded the configured size limit")
        output = dict(response)
        cleanup_arguments = output.pop("_cleanup_arguments", None)
        spec = CAPABILITY_CATALOG[call.capability]
        if spec.cleanup_required:
            if not isinstance(cleanup_arguments, Mapping):
                raise ToolProtocolError(
                    "cleanup-required MCP tool must return _cleanup_arguments"
                )
        elif cleanup_arguments is not None:
            raise ToolProtocolError(
                "MCP tool returned cleanup arguments for a read-only capability"
            )
        return ToolExecutionResult(
            call_id=call.call_id,
            backend=ToolBackend.MCP,
            tool_name=descriptor.tool_name,
            output=output,
            cleanup_tool_name=descriptor.cleanup_tool_name,
            cleanup_arguments=(
                dict(cleanup_arguments)
                if isinstance(cleanup_arguments, Mapping)
                else None
            ),
        )

    async def execute_cleanup(
        self, task: CleanupTask, context: ProbeContext
    ) -> dict[str, Any]:
        if task.run_id != context.run_id:
            raise ToolProtocolError("cleanup task run_id does not match its context")
        binding = self._bindings.get(task.capability)
        if binding is None or binding.cleanup_tool_name is None:
            raise ProbeRegistrationError(
                f"no MCP cleanup tool registered: {task.capability.value}"
            )
        expected = f"{binding.server_name}.{binding.cleanup_tool_name}"
        if task.tool_name != expected:
            raise ToolProtocolError("cleanup task does not match its MCP binding")
        if self.transport is None:
            raise ToolTransportUnavailable("MCP transport is not configured")
        response = await self.transport.call_tool(
            binding.server_name,
            binding.cleanup_tool_name,
            {
                "cleanup_id": task.cleanup_id,
                "run_id": task.run_id,
                "parent_action_id": task.parent_action_id,
                "capability": task.capability.value,
                "arguments": task.arguments,
            },
        )
        if not isinstance(response, Mapping):
            raise ToolProtocolError("MCP cleanup response must be an object")
        return dict(response)


class CompositeToolAdapter:
    """Routes each capability to exactly one adapter and rejects ambiguous bindings."""

    def __init__(self, adapters: tuple[ToolAdapter, ...], capabilities: set[Capability]) -> None:
        self._routes: dict[Capability, ToolAdapter] = {}
        for capability in capabilities:
            matches: list[ToolAdapter] = []
            for adapter in adapters:
                try:
                    adapter.resolve(capability)
                except ProbeRegistrationError:
                    continue
                matches.append(adapter)
            if len(matches) > 1:
                raise ProbeRegistrationError(
                    f"multiple tool adapters registered: {capability.value}"
                )
            if matches:
                self._routes[capability] = matches[0]

    def resolve(self, capability: Capability) -> ToolDescriptor:
        try:
            adapter = self._routes[capability]
        except KeyError as exc:
            raise ProbeRegistrationError(
                f"no tool adapter registered for capability: {capability.value}"
            ) from exc
        return adapter.resolve(capability)

    async def execute(
        self, call: ToolCall, context: ProbeContext
    ) -> ToolExecutionResult:
        try:
            adapter = self._routes[call.capability]
        except KeyError as exc:
            raise ProbeRegistrationError(
                f"no tool adapter registered for capability: {call.capability.value}"
            ) from exc
        return await adapter.execute(call, context)

    async def execute_cleanup(
        self, task: CleanupTask, context: ProbeContext
    ) -> dict[str, Any]:
        try:
            adapter = self._routes[task.capability]
        except KeyError as exc:
            raise ProbeRegistrationError(
                f"no cleanup adapter registered: {task.capability.value}"
            ) from exc
        return await adapter.execute_cleanup(task, context)
