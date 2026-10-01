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
from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from redteam.capability.adapters.mcp.base import MCPAdapter, MCPCallResult
from redteam.capability.adapters.mcp.workspace import WorkspaceMCPAdapter
from redteam.capability.catalog import MCPToolCatalog, ToolCandidateMetadata
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId

# Health is a liveness signal, not an authorization decision. A single probe
# is always bounded by this timeout so a hung MCP server can never stall an
# already-approved invocation. ``0`` for the interval means "probe before
# every invocation"; a positive interval throttles repeated probes per server.
DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS = 0.0
DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS = 5.0


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
            server_id: asyncio.Semaphore(concurrency.get(server_id, 1))
            for server_id in adapters
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
        return self._registry.get(tool_id)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return self._registry.list_by_capability(capability_tag)

    def all(self) -> list[ToolDescriptor]:
        """Every registered tool, regardless of capability — read-only
        catalog enumeration for callers like `redteam tools list`."""
        return self._registry.all()

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
        descriptor = self._registry.get(invocation.tool_id)
        if descriptor is None:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                error=ToolError(
                    code="unknown_tool", message=f"tool {invocation.tool_id} not registered"
                ),
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

        adapter = self._adapters.get(descriptor.server_id)
        if adapter is None:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                error=ToolError(
                    code="unknown_server",
                    message=f"no adapter registered for server {descriptor.server_id}",
                ),
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

        if not self._catalog.supports(invocation.tool_id, invocation.operation):
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
                invocation_id=invocation.invocation_id, action_id=invocation.action_id,
                tool_id=invocation.tool_id, success=True,
                output={"dry_run": True, "executed": False},
                started_at=occurred_at, finished_at=occurred_at,
                metadata={"dry_run": True},
            )

        # Refresh the discovery-time health snapshot before deciding. This is
        # a bounded liveness probe, not authorization: the Control Plane has
        # already allowed this invocation. A failed/timed-out probe leaves the
        # last known status in place and never by itself turns into an error.
        if self._health_refresh_enabled:
            now = datetime.now(timezone.utc)
            if self._needs_health_refresh(descriptor.server_id, now):
                await self._refresh_server_health(descriptor.server_id)
                refreshed = self._registry.get(invocation.tool_id)
                if refreshed is not None:
                    descriptor = refreshed

        if descriptor.health_status == ToolHealthStatus.UNHEALTHY:
            return self._error_result(
                invocation,
                code="tool_unhealthy",
                message=f"tool {invocation.tool_id} belongs to an unhealthy MCP server",
                retryable=True,
            )

        arguments = invocation.arguments
        if isinstance(adapter, WorkspaceMCPAdapter):
            try:
                arguments = adapter.bind_context(invocation)
            except ValueError:
                return self._error_result(
                    invocation,
                    code="workspace_context_mismatch",
                    message="workspace invocation context differs from trusted execution context",
                )
            try:
                properties = descriptor.input_schema.get("properties", {})
                if set(arguments) - set(properties):
                    raise ValueError("unexpected workspace arguments")
                Draft202012Validator(descriptor.input_schema).validate(arguments)
            except (SchemaError, ValidationError, TypeError, ValueError):
                return self._error_result(
                    invocation,
                    code="workspace_invalid_arguments",
                    message="workspace arguments do not match the registered input schema",
                )

        try:
            adapter_result = await asyncio.wait_for(
                _invoke_with_limit(
                    adapter,
                    descriptor,
                    arguments,
                    self._server_semaphores[descriptor.server_id],
                ),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._error_result(
                invocation,
                code="tool_timeout",
                message=(
                    "tool queue and execution exceeded timeout of "
                    f"{invocation.timeout_seconds} seconds"
                ),
                retryable=True,
            )
        except Exception as exc:
            return self._error_result(
                invocation,
                code="adapter_error",
                message=f"adapter execution failed: {type(exc).__name__}",
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
                metadata=adapter_result.metadata,
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
            metadata=adapter_result.metadata,
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

    @staticmethod
    def _error_result(
        invocation: ToolInvocation,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> ToolResult:
        occurred_at = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=False,
            error=ToolError(code=code, message=message, retryable=retryable),
            started_at=occurred_at,
            finished_at=occurred_at,
        )


async def _invoke_with_limit(
    adapter: MCPAdapter,
    descriptor: ToolDescriptor,
    arguments: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> MCPCallResult:
    async with semaphore:
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
