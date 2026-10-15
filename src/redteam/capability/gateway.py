"""Default ``ToolGateway`` implementation. Owned by Capability Team.

Routes an already-approved ``ToolInvocation`` to the ``MCPAdapter`` that
owns the target tool's ``server_id``. This is the only class in the
codebase allowed to call an ``MCPAdapter`` directly.

Retries remain deliberately disabled because a generic gateway cannot
assume that a tool invocation is idempotent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from redteam.capability.adapters.mcp.base import (
    ContextualInvocationAdapter,
    InvocationContextBinder,
    MCPAdapter,
    MCPCallResult,
)
from redteam.capability.catalog import MCPToolCatalog, ToolCandidateMetadata
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ToolDescriptor,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from redteam.core.convert import execution_input_digest
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId
from redteam.core.ports.execution_recovery import ExecutionRecoveryProvider
from redteam.core.ports.tool_catalog import ToolCard, ToolCardKind, ToolDetail
from redteam.core.ports.tool_gateway import BeforeToolStart, ToolStartRejected
from redteam.core.tool_binding import SELECTED_TOOL

# Health is a liveness signal, not an authorization decision. A single probe
# is always bounded by this timeout so a hung MCP server can never stall an
# already-approved invocation. ``0`` for the interval means "probe before
# every invocation"; a positive interval throttles repeated probes per server.
DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS = 0.0
DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS = 5.0
# Fixed stage/owner labels for gateway-raised failures. These name the
# owning stage module, never a tool/server name, and never copy inputs.
_GATEWAY_STAGE = "gateway"
_GATEWAY_SOURCE = "gateway"


class MCPToolGateway:
    def __init__(
        self,
        registry: MCPToolRegistry,
        adapters: dict[str, MCPAdapter],
        operation_bindings: Mapping[str, Collection[str]] | None = None,
        *,
        server_concurrency: Mapping[str, int] | None = None,
        server_output_limits: Mapping[str, int] | None = None,
        output_schemas: Mapping[str, dict[str, Any] | None] | None = None,
        target_types: Mapping[str, str | None] | None = None,
        health_refresh_enabled: bool = True,
        health_refresh_interval_seconds: float = DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS,
        health_refresh_timeout_seconds: float = DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self._catalog = MCPToolCatalog(
            registry,
            operation_bindings,
            output_schemas=output_schemas,
            target_types=target_types,
        )
        concurrency = server_concurrency or {}
        unknown_servers = set(concurrency) - set(adapters)
        if unknown_servers:
            names = ", ".join(sorted(unknown_servers))
            raise ValueError(f"concurrency configured for unknown MCP servers: {names}")
        if any(limit < 1 for limit in concurrency.values()):
            raise ValueError("MCP server concurrency must be at least 1")
        self._server_semaphores = {
            server_id: asyncio.Semaphore(concurrency.get(server_id, 1)) for server_id in adapters
        }
        output_limits = server_output_limits or {}
        unknown_output_servers = set(output_limits) - set(adapters)
        if unknown_output_servers:
            names = ", ".join(sorted(unknown_output_servers))
            raise ValueError(f"output limit configured for unknown MCP servers: {names}")
        if any(limit < 1 for limit in output_limits.values()):
            raise ValueError("MCP server output limit must be at least 1 byte")
        self._server_output_limits = {
            server_id: output_limits.get(server_id, 1_048_576) for server_id in adapters
        }
        if health_refresh_interval_seconds < 0:
            raise ValueError("health refresh interval must be >= 0 seconds")
        if health_refresh_timeout_seconds <= 0:
            raise ValueError("health refresh timeout must be > 0 seconds")
        self._health_refresh_enabled = health_refresh_enabled
        self._health_refresh_interval = float(health_refresh_interval_seconds)
        self._health_refresh_timeout = float(health_refresh_timeout_seconds)
        # Discovery just probed every adapter, so that probe counts as the
        # first refresh instant; a positive interval can then skip repeats.
        bootstrapped_at = datetime.now(timezone.utc)
        self._last_health_refresh: dict[str, datetime] = {
            server_id: bootstrapped_at for server_id in adapters
        }

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        """Expose a defensive catalog snapshot to the application composition root."""
        return self._catalog.get(tool_id)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return self._catalog.list_by_capability(capability_tag)

    def all_descriptors(self) -> list[ToolDescriptor]:
        """Expose the complete registered catalog through the public port."""
        return self._catalog.all_descriptors()

    def all(self) -> list[ToolDescriptor]:
        """Every registered tool, regardless of capability — read-only
        catalog enumeration for callers like `redteam tools list`."""
        return [self._catalog.finalized(item) for item in self._registry.all()]

    def health_snapshot(self) -> dict[str, ToolHealthStatus]:
        """Current per-server health from the live Registry (no probing)."""

        return self._catalog.health_snapshot()

    def last_health_refresh(self, server_id: str) -> datetime | None:
        """When this gateway last *attempted* a health probe for a server.

        Recorded even for failed/timed-out probes so the interval throttles a
        persistently unreachable server instead of retrying it unboundedly.
        """

        return self._last_health_refresh.get(server_id)

    async def refresh_health(
        self,
        server_ids: Collection[str] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, ToolHealthStatus]:
        """Probe server health, update the Registry, and return the result.

        This is the planner-facing refresh path: call it before
        ``list_tools_for_action`` so candidate selection sees a live status
        instead of the discovery-time snapshot. Every probe is bounded by
        ``health_refresh_timeout_seconds``; a timeout or transport failure
        keeps the last known status rather than failing the refresh.

        Health is *not* authorization: this method never consults the Control
        Plane and never blocks a call. It only updates a liveness signal.
        """

        selected = list(self._adapters) if server_ids is None else list(server_ids)
        unknown = set(selected) - set(self._adapters)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"health refresh requested for unknown MCP servers: {names}")
        if not self._health_refresh_enabled:
            return self._health_statuses(selected)
        now = datetime.now(timezone.utc)
        pending = [
            server_id
            for server_id in selected
            if force or self._needs_health_refresh(server_id, now)
        ]
        if pending:
            await asyncio.gather(
                *(self._refresh_server_health(server_id) for server_id in pending),
                return_exceptions=True,
            )
        return self._health_statuses(selected)

    def _health_statuses(self, server_ids: Collection[str]) -> dict[str, ToolHealthStatus]:
        statuses: dict[str, ToolHealthStatus] = {}
        for server_id in server_ids:
            status = self._registry.server_health(server_id)
            statuses[server_id] = status if status is not None else ToolHealthStatus.UNKNOWN
        return statuses

    def _needs_health_refresh(self, server_id: str, now: datetime) -> bool:
        if self._health_refresh_interval <= 0:
            return True
        last = self._last_health_refresh.get(server_id)
        if last is None:
            return True
        return (now - last).total_seconds() >= self._health_refresh_interval

    async def _refresh_server_health(self, server_id: str) -> ToolHealthStatus | None:
        adapter = self._adapters[server_id]
        # Record the attempt before awaiting so a hung probe still throttles.
        self._last_health_refresh[server_id] = datetime.now(timezone.utc)
        try:
            status = await asyncio.wait_for(
                adapter.health_check(), timeout=self._health_refresh_timeout
            )
        except Exception:  # noqa: BLE001 - a probe failure must never raise into a caller
            return None
        self._registry.update_server_health(server_id, status)
        return status

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        return await self._invoke(invocation)

    def prepare_reference(self, invocation: ToolInvocation) -> ExecutionReference | None:
        """Derive a recovery handle without invoking or probing any provider."""
        descriptor = self._catalog.get(invocation.tool_id)
        if descriptor is None or invocation.execution_context.dry_run:
            return None
        adapter = self._adapters.get(descriptor.server_id)
        if not isinstance(adapter, ExecutionRecoveryProvider):
            return None
        if (invocation.binding_mode != SELECTED_TOOL
            and not self._catalog.supports(invocation.tool_id, invocation.operation)):
            raise ValueError("execution_reference_operation_mismatch")
        bound = self._bind_invocation(invocation, descriptor, adapter)
        if isinstance(bound, ToolResult):
            # No provider entry is possible for this rejected input. Leave it
            # handle-less so ordinary invoke returns the structured error to
            # the worker, rather than aborting submission before that reply.
            return None
        reference = adapter.prepare_reference(bound)
        if reference is not None:
            expected = {
                "engagement_id": bound.execution_context.engagement_id,
                "run_id": bound.run_id,
                "instance_id": bound.execution_context.instance_id,
                "action_id": bound.action_id,
                "invocation_id": bound.invocation_id,
                "tool_id": bound.tool_id,
                "input_digest": execution_input_digest(bound),
            }
            if any(getattr(reference, key) != value for key, value in expected.items()):
                raise ValueError("execution_reference_identity_mismatch")
        return reference

    def _recovery_provider(self, reference: ExecutionReference) -> ExecutionRecoveryProvider | None:
        descriptor = self._catalog.get(reference.tool_id)
        adapter = self._adapters.get(descriptor.server_id) if descriptor is not None else None
        return adapter if isinstance(adapter, ExecutionRecoveryProvider) else None

    async def query_execution(self, reference: ExecutionReference) -> ExecutionRecoveryAnswer:
        provider = self._recovery_provider(reference)
        if provider is None:
            return ExecutionRecoveryAnswer(
                state=ExecutionState.UNKNOWN,
                error=ToolError(code="execution_recovery_unavailable",
                                message="the registered provider cannot recover this execution"),
            )
        try:
            return await asyncio.wait_for(provider.query_execution(reference),
                                          timeout=self._health_refresh_timeout)
        except Exception as exc:
            return ExecutionRecoveryAnswer(
                state=ExecutionState.UNKNOWN,
                error=ToolError(code="execution_recovery_query_failed",
                                message="provider execution state could not be verified",
                                details={"exception_type": type(exc).__name__[:100]}),
            )

    async def fetch_execution(self, reference: ExecutionReference) -> ToolResult | None:
        provider = self._recovery_provider(reference)
        if provider is None:
            return None
        # Failures propagate to the recovery consumer, which must preserve
        # its unsettled receipt. Absence never authorizes another invocation.
        result = await asyncio.wait_for(provider.fetch_execution(reference),
                                        timeout=self._health_refresh_timeout)
        if result is None:
            return None
        if (result.invocation_id != reference.invocation_id
            or result.action_id != reference.action_id or result.tool_id != reference.tool_id):
            raise ValueError("execution_recovery_result_identity_mismatch")
        descriptor = self._catalog.get(reference.tool_id)
        if descriptor is None:
            return None
        limit = self._server_output_limits[descriptor.server_id]
        if _serialized_output_size(result.output) > limit:
            raise ValueError("execution_recovery_output_too_large")
        if result.success and descriptor.output_schema is not None:
            Draft202012Validator(descriptor.output_schema).validate(result.output)
        return result

    async def invoke_with_admission(
        self,
        invocation: ToolInvocation,
        before_start: BeforeToolStart,
    ) -> ToolResult:
        return await self._invoke(invocation, before_start=before_start)

    def _bind_invocation(
        self, invocation: ToolInvocation, descriptor: ToolDescriptor, adapter: MCPAdapter
    ) -> ToolInvocation | ToolResult:
        arguments = invocation.arguments
        if isinstance(adapter, InvocationContextBinder):
            # Diagnostic naming is optional; it must not decide whether
            # trusted identity binding runs for a provider.
            error_prefix = getattr(adapter, "error_prefix", "provider")
            try:
                arguments = adapter.bind_context(invocation)
            except ValueError:
                return self._error_result(
                    invocation,
                    code=f"{error_prefix}_context_mismatch",
                    message="provider invocation context differs from trusted execution context",
                )
            except Exception as exc:
                return self._error_result(
                    invocation,
                    code=f"{error_prefix}_context_binding_failed",
                    message="provider could not bind the trusted invocation context",
                    exc=exc,
                )
            try:
                properties = descriptor.input_schema.get("properties", {})
                if set(arguments) - set(properties):
                    raise ValueError("unexpected scoped arguments")
                Draft202012Validator(descriptor.input_schema).validate(arguments)
            except (SchemaError, ValidationError, TypeError, ValueError):
                return self._error_result(
                    invocation,
                    code=f"{error_prefix}_invalid_arguments",
                    message="bound arguments do not match the registered input schema",
                )

        return invocation.model_copy(update={"arguments": dict(arguments)})

    async def _invoke(
        self,
        invocation: ToolInvocation,
        *,
        before_start: BeforeToolStart | None = None,
    ) -> ToolResult:
        descriptor = self._catalog.get(invocation.tool_id)
        if descriptor is None:
            return self._error_result(
                invocation,
                code="unknown_tool",
                message=f"tool {invocation.tool_id} not registered",
            )

        adapter = self._adapters.get(descriptor.server_id)
        if adapter is None:
            return self._error_result(
                invocation,
                code="unknown_server",
                message=f"no adapter registered for server {descriptor.server_id}",
            )

        legacy_blocked = invocation.binding_mode != SELECTED_TOOL
        if legacy_blocked:
            allowed = self._catalog.supports(invocation.tool_id, invocation.operation)
        else:
            allowed = True
        if not allowed:
            return self._error_result(
                invocation,
                code="operation_not_allowed_for_tool",
                message=(
                    f"tool {invocation.tool_id} is not registered for canonical operation "
                    f"{invocation.operation!r}"
                ),
            )

        if invocation.execution_context.dry_run:
            occurred_at = datetime.now(timezone.utc)
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=True,
                output={"dry_run": True, "executed": False},
                started_at=occurred_at,
                finished_at=occurred_at,
                metadata={"dry_run": True, EXECUTION_STARTED_METADATA_KEY: False},
            )

        # Refresh the discovery-time health snapshot before deciding. This is
        # a bounded liveness probe, not authorization: the Control Plane has
        # already allowed this invocation. A failed/timed-out probe leaves the
        # last known status in place and never by itself turns into an error.
        if self._health_refresh_enabled:
            now = datetime.now(timezone.utc)
            if self._needs_health_refresh(descriptor.server_id, now):
                await self._refresh_server_health(descriptor.server_id)
                refreshed = self._catalog.get(invocation.tool_id)
                if refreshed is not None:
                    descriptor = refreshed

        if descriptor.health_status == ToolHealthStatus.UNHEALTHY:
            return self._error_result(
                invocation,
                code="tool_unhealthy",
                message=f"tool {invocation.tool_id} belongs to an unhealthy MCP server",
                retryable=True,
            )

        bound = self._bind_invocation(invocation, descriptor, adapter)
        if isinstance(bound, ToolResult):
            return bound
        arguments = bound.arguments

        entered = False

        def provider_entered() -> None:
            nonlocal entered
            entered = True

        try:
            adapter_result = await asyncio.wait_for(
                _invoke_with_limit(
                    adapter,
                    descriptor,
                    arguments,
                    self._server_semaphores[descriptor.server_id],
                    invocation=bound,
                    on_provider_entry=provider_entered,
                    before_start=before_start,
                ),
                timeout=invocation.timeout_seconds,
            )
        except ToolStartRejected:
            raise
        except asyncio.TimeoutError:
            return self._error_result(
                invocation,
                code="tool_timeout",
                message=(
                    "tool queue and execution exceeded timeout of "
                    f"{invocation.timeout_seconds} seconds"
                ),
                retryable=not entered,
                execution_started=entered,
                uncertain=entered,
            )
        except Exception as exc:
            return self._error_result(
                invocation,
                code="adapter_error",
                message=f"adapter execution failed: {type(exc).__name__}",
                exc=exc,
                execution_started=entered,
                uncertain=entered,
            )

        if not adapter_result.success:
            # A deliberate MCP tool-level rejection (e.g. `ToolError` on the
            # server, surfaced here as `adapter_result.error`) is not a
            # successful call with malformed output -- the size/output-schema
            # checks below exist to validate a *successful* tool's output,
            # and applying them to error content (never shaped like the
            # success schema) always failed the schema check, discarding the
            # adapter's already-correct error in favor of an opaque
            # "MCP output did not match the registered output schema" that
            # named the wrong problem.
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                output=adapter_result.output,
                error=adapter_result.error,
                started_at=adapter_result.started_at,
                finished_at=adapter_result.finished_at,
                metadata={**adapter_result.metadata, EXECUTION_STARTED_METADATA_KEY: True},
            )

        output_bytes = _serialized_output_size(adapter_result.output)
        max_output_bytes = self._server_output_limits[descriptor.server_id]
        if output_bytes > max_output_bytes:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                output=None,
                error=ToolError(
                    code="tool_output_too_large",
                    message="tool output exceeded the configured byte limit and was omitted",
                    retryable=False,
                ),
                started_at=adapter_result.started_at,
                finished_at=adapter_result.finished_at,
                metadata={
                    EXECUTION_STARTED_METADATA_KEY: True,
                    "output_omitted": True,
                    "output_bytes": output_bytes,
                    "max_output_bytes": max_output_bytes,
                },
            )

        if descriptor.output_schema is not None:
            try:
                Draft202012Validator(descriptor.output_schema).validate(adapter_result.output)
            except (SchemaError, ValidationError, TypeError):
                return self._error_result(
                    invocation,
                    code="tool_output_schema_mismatch",
                    execution_started=True,
                    message="MCP output did not match the registered output schema",
                )

        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=adapter_result.success,
            output=adapter_result.output,
            error=adapter_result.error,
            started_at=adapter_result.started_at,
            finished_at=adapter_result.finished_at,
            metadata={**adapter_result.metadata, EXECUTION_STARTED_METADATA_KEY: True},
        )

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolDescriptor]:
        """Return eligible Registry candidates without applying selection policy."""

        return self._catalog.list_tools_for_action(
            capability,
            operation,
            include_unhealthy=include_unhealthy,
        )

    def list_candidate_metadata(
        self,
        capability: str | None = None,
        operation: str | None = None,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolCandidateMetadata]:
        """Describe registered candidates without selecting or invoking them."""

        return self._catalog.list_candidate_metadata(
            capability,
            operation,
            include_unhealthy=include_unhealthy,
        )

    def list_cards(
        self,
        *,
        kind: ToolCardKind | None = None,
        capability: str | None = None,
        query: str | None = None,
        include_unhealthy: bool = False,
    ) -> list[ToolCard]:
        return self._catalog.list_cards(
            kind=kind,
            capability=capability,
            query=query,
            include_unhealthy=include_unhealthy,
        )

    def get_detail(self, tool_id: ToolId) -> ToolDetail | None:
        return self._catalog.get_detail(tool_id)

    @staticmethod
    def _error_result(
        invocation: ToolInvocation,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        exc: BaseException | None = None,
        execution_started: bool = False,
        uncertain: bool = False,
    ) -> ToolResult:
        occurred_at = datetime.now(timezone.utc)
        details: dict[str, object] = {
            "stage": _GATEWAY_STAGE,
            "source": _GATEWAY_SOURCE,
        }
        if exc is not None:
            details["exception_type"] = type(exc).__name__[:100]
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=False,
            error=ToolError(
                code=code, message=message, retryable=retryable, details=details
            ),
            started_at=occurred_at,
            finished_at=occurred_at,
            metadata={
                EXECUTION_STARTED_METADATA_KEY: execution_started,
                **({EXECUTION_UNCERTAIN_METADATA_KEY: True} if uncertain else {}),
            },
        )


async def _invoke_with_limit(
    adapter: MCPAdapter,
    descriptor: ToolDescriptor,
    arguments: dict[str, Any],
    semaphore: asyncio.Semaphore,
    *,
    invocation: ToolInvocation,
    on_provider_entry: Callable[[], None],
    before_start: BeforeToolStart | None = None,
) -> MCPCallResult:
    async with semaphore:
        if before_start is not None:
            await before_start()
        on_provider_entry()
        if isinstance(adapter, ContextualInvocationAdapter):
            return await adapter.invoke_contextual(invocation)
        return await adapter.invoke(descriptor, arguments)


def _serialized_output_size(output: dict[str, Any] | str | None) -> int:
    if output is None:
        return 0
    if isinstance(output, str):
        return len(output.encode("utf-8"))
    serialized = json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return len(serialized.encode("utf-8"))
