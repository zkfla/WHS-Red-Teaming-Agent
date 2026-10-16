"""Capability-owned construction helper used by the application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

from redteam.capability.action_binding import declares_retry_safe
from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
from redteam.capability.adapters.mcp.transport import (
    AllowlistedMCPTransport,
    MCPTransport,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
)
from redteam.capability.gateway import (
    DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS,
    DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS,
    MCPToolGateway,
)
from redteam.capability.manifest import split_tool_id, validate_tool_manifest
from redteam.capability.registration import (
    McpProfileBundle,
    apply_profile_discovery_binding,
    apply_server_catalog_target_types,
    create_adapter_for_settings,
    default_target_types_for_kind,
    project_default_target_binding,
    select_server_target_type,
    validate_server_configured_discovery,
)
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.settings import (
    MCPConfigurationError,
    MCPServerKind,
    MCPServerSettings,
    MCPTransportKind,
)


async def build_mcp_tool_gateway(
    server_ids: list[str],
    *,
    health_refresh_enabled: bool = True,
    health_refresh_interval_seconds: float = DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS,
    health_refresh_timeout_seconds: float = DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS,
    profile_bundles: Mapping[str, McpProfileBundle] | None = None,
    env: Mapping[str, str] | None = None,
    builtin_adapters: Sequence[MCPAdapter] = (),
    builtin_concurrency: Mapping[str, int] | None = None,
    builtin_output_limits: Mapping[str, int] | None = None,
    trusted_termination_servers: Sequence[str] = (),
) -> MCPToolGateway:
    """Discover configured servers and return a ready-to-invoke gateway.

    An empty list intentionally preserves the deterministic in-process example
    used by the repository's minimal vertical slice. Health-refresh settings
    are forwarded to the Gateway so the composition root can tune the bounded
    liveness probe without widening the frozen Core config contract.
    """

    return await _build_gateway(
        server_ids,
        health_refresh_enabled=health_refresh_enabled,
        health_refresh_interval_seconds=health_refresh_interval_seconds,
        health_refresh_timeout_seconds=health_refresh_timeout_seconds,
        profile_bundles=profile_bundles,
        env=env,
        builtin_adapters=builtin_adapters,
        builtin_concurrency=builtin_concurrency,
        builtin_output_limits=builtin_output_limits,
        trusted_termination_servers=trusted_termination_servers,
    )


@asynccontextmanager
async def open_mcp_tool_gateway(
    server_ids: list[str],
    *,
    health_refresh_enabled: bool = True,
    health_refresh_interval_seconds: float = DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS,
    health_refresh_timeout_seconds: float = DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS,
    profile_bundles: Mapping[str, McpProfileBundle] | None = None,
    env: Mapping[str, str] | None = None,
    builtin_adapters: Sequence[MCPAdapter] = (),
    builtin_concurrency: Mapping[str, int] | None = None,
    builtin_output_limits: Mapping[str, int] | None = None,
    trusted_termination_servers: Sequence[str] = (),
) -> AsyncIterator[MCPToolGateway]:
    """Engagement-scoped connections, required for stateful browser operations.

    The orchestration owner must enter/exit in the same task and finish all
    invocation tasks before exiting. Never share this scope across engagements.
    Partial startup failures close previously opened connections as well.
    """
    async with AsyncExitStack() as stack:
        yield await _build_gateway(
            server_ids,
            stack,
            health_refresh_enabled=health_refresh_enabled,
            health_refresh_interval_seconds=health_refresh_interval_seconds,
            health_refresh_timeout_seconds=health_refresh_timeout_seconds,
            profile_bundles=profile_bundles,
            env=env,
            builtin_adapters=builtin_adapters,
            builtin_concurrency=builtin_concurrency,
            builtin_output_limits=builtin_output_limits,
            trusted_termination_servers=trusted_termination_servers,
        )


async def _build_gateway(
    server_ids: list[str],
    stack: AsyncExitStack | None = None,
    *,
    health_refresh_enabled: bool = True,
    health_refresh_interval_seconds: float = DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS,
    health_refresh_timeout_seconds: float = DEFAULT_HEALTH_REFRESH_TIMEOUT_SECONDS,
    profile_bundles: Mapping[str, McpProfileBundle] | None = None,
    env: Mapping[str, str] | None = None,
    builtin_adapters: Sequence[MCPAdapter] = (),
    builtin_concurrency: Mapping[str, int] | None = None,
    builtin_output_limits: Mapping[str, int] | None = None,
    trusted_termination_servers: Sequence[str] = (),
) -> MCPToolGateway:
    bundles = dict(profile_bundles) if profile_bundles is not None else None
    from redteam.core.environment import env_snapshot

    environment = env_snapshot(env)
    builtin_ids = {adapter.server_id for adapter in builtin_adapters}
    trusted_termination = set(trusted_termination_servers)
    if trusted_termination - (set(server_ids) | builtin_ids):
        raise MCPConfigurationError("termination trust requires a registered provider")
    configured_concurrency = dict(builtin_concurrency or {})
    configured_output_limits = dict(builtin_output_limits or {})
    if set(configured_concurrency) - builtin_ids:
        raise MCPConfigurationError("concurrency configured for an unregistered built-in provider")
    if any(limit < 1 for limit in configured_concurrency.values()):
        raise MCPConfigurationError("built-in provider concurrency must be at least 1")
    if set(configured_output_limits) - builtin_ids:
        raise MCPConfigurationError("output limit configured for an unregistered built-in provider")
    if any(limit < 1 for limit in configured_output_limits.values()):
        raise MCPConfigurationError("built-in provider output limit must be at least 1 byte")
    if not server_ids and not builtin_adapters:
        example = ExampleMCPAdapter()
        registry = MCPToolRegistry()
        example_descriptors = await example.discover_tools()
        for descriptor in example_descriptors:
            validate_tool_manifest(descriptor)
        registry.replace_server_tools(example.server_id, example_descriptors)
        return MCPToolGateway(
            registry,
            {example.server_id: example},
            {str(tool.tool_id): ("service_discovery",) for tool in registry.all()},
            health_refresh_enabled=health_refresh_enabled,
            health_refresh_interval_seconds=health_refresh_interval_seconds,
            health_refresh_timeout_seconds=health_refresh_timeout_seconds,
        )

    if len(server_ids) != len(set(server_ids)):
        raise MCPConfigurationError("CapabilityConfig.mcp_servers contains duplicates")

    registry = MCPToolRegistry()
    adapters: dict[str, MCPAdapter] = {}
    operation_bindings: dict[str, tuple[str, ...]] = {}
    server_concurrency: dict[str, int] = {}
    server_output_limits: dict[str, int] = {}
    output_schemas: dict[str, dict | None] = {}
    target_types: dict[str, str | None] = {}
    for server_id in server_ids:
        settings = MCPServerSettings.from_environment(server_id, bundles=bundles, env=environment)
        transport = _create_transport(settings)
        if stack is not None:
            await stack.enter_async_context(transport.session())
        adapter = _create_adapter(settings, transport, bundles)
        if server_id in trusted_termination:
            if not isinstance(adapter, MCPServerAdapter):
                raise MCPConfigurationError(
                    "provider does not support verified termination metadata"
                )
            adapter.configure_execution_termination_trust(True)
        descriptors = await adapter.discover_tools()
        if not descriptors:
            raise MCPConfigurationError(f"MCP server {server_id!r} discovered no tools")
        if set(settings.tool_resources) - {tool.tool_name for tool in descriptors}:
            raise MCPConfigurationError("resource bindings reference undiscovered tools")
        if settings.tool_operations is not None:
            discovered_names = {descriptor.tool_name for descriptor in descriptors}
            configured_names = set(settings.tool_operations)
            unknown_names = configured_names - discovered_names
            if unknown_names:
                raise MCPConfigurationError(
                    f"MCP server {server_id!r} operation bindings reference undiscovered tools"
                )
        server_bindings: dict[str, tuple[str, ...]] = {}
        for descriptor in descriptors:
            # Operator declarations ADD locks; they cannot erase provider locks.
            for binding in settings.tool_resources.get(descriptor.tool_name, []):
                if binding not in descriptor.resource_bindings:
                    descriptor.resource_bindings.append(binding)
            if len(descriptor.resource_bindings) > 32:
                raise MCPConfigurationError("too many combined resource declarations")
            configured = (
                settings.tool_operations.get(descriptor.tool_name)
                if settings.tool_operations is not None
                else None
            )
            if configured is not None:
                operations = tuple(configured)
                if descriptor.operations and set(operations) != set(descriptor.operations):
                    raise MCPConfigurationError(
                        f"MCP server {server_id!r} tool {descriptor.tool_name!r} explicit "
                        "operation bindings conflict with discovered operations"
                    )
            elif descriptor.operations:
                operations = tuple(descriptor.operations)
            else:
                # Unbound tools carry no legacy operation intent; they stay
                # registered and addressable by direct selected-tool calls.
                operations = ()
            # The operation binding is part of the Registry snapshot exposed
            # to Agent/Orchestration; it must not remain only in a private
            # Gateway map.
            descriptor.operations = list(operations)
            project_default_target_binding(descriptor, operations)
            split = split_tool_id(descriptor.tool_id)
            descriptor.retry_safe = declares_retry_safe(
                split[0] if split is not None else "", operations
            )
            selected_target = select_server_target_type(server_id, str(descriptor.tool_id))
            descriptor.target_types = (
                [selected_target]
                if selected_target is not None
                else descriptor.target_types or default_target_types_for_kind(settings.kind)
            )
            profile = (
                (bundles or {}).get(settings.profile)
                if settings.profile is not None
                else None
            )
            if profile is not None and profile.default_target_types and selected_target is None:
                descriptor.target_types = list(profile.default_target_types)
            if settings.profile is not None:
                catalog_target = apply_profile_discovery_binding(descriptor, settings, bundles)
                if catalog_target is not None:
                    target_types[str(descriptor.tool_id)] = catalog_target
            # Validate after operator bindings are copied into the descriptor:
            # this is the exact manifest exposed to Agent/Orchestration.
            validate_tool_manifest(descriptor)
            operation_bindings[str(descriptor.tool_id)] = operations
            server_bindings[str(descriptor.tool_id)] = operations
            if isinstance(adapter, MCPServerAdapter):
                schema = adapter.output_schema_for_tool(descriptor.tool_id)
                if schema is not None:
                    output_schemas[str(descriptor.tool_id)] = schema
        if settings.tool_operations is not None:
            server_schemas = {
                descriptor.tool_name: output_schemas.get(str(descriptor.tool_id))
                for descriptor in descriptors
            }
            validate_server_configured_discovery(
                server_id, descriptors, server_bindings, server_schemas
            )
        apply_server_catalog_target_types(server_id, server_bindings, target_types)
        registry.replace_server_tools(server_id, descriptors)
        adapters[server_id] = adapter
        server_concurrency[server_id] = settings.max_concurrency
        server_output_limits[server_id] = settings.max_output_bytes

    # In-process providers share discovery, catalog, admission and result
    # handling with connected providers. They do not acquire an MCP transport.
    # Registration happens before constructing the gateway and its catalog.
    for adapter in builtin_adapters:
        server_id = adapter.server_id
        if server_id in adapters:
            raise MCPConfigurationError(f"duplicate provider registration: {server_id!r}")
        descriptors = await adapter.discover_tools()
        if not descriptors:
            raise MCPConfigurationError(f"provider {server_id!r} discovered no tools")
        for descriptor in descriptors:
            if descriptor.server_id != server_id:
                raise MCPConfigurationError("provider descriptor belongs to a different server")
            validate_tool_manifest(descriptor)
            operation_bindings[str(descriptor.tool_id)] = tuple(descriptor.operations)
        registry.replace_server_tools(server_id, descriptors)
        adapters[server_id] = adapter

        server_concurrency[server_id] = configured_concurrency.get(server_id, 1)
        if server_id in configured_output_limits:
            server_output_limits[server_id] = configured_output_limits[server_id]

    return MCPToolGateway(
        registry,
        adapters,
        operation_bindings,
        server_concurrency=server_concurrency,
        server_output_limits=server_output_limits,
        output_schemas=output_schemas,
        target_types=target_types,
        health_refresh_enabled=health_refresh_enabled,
        health_refresh_interval_seconds=health_refresh_interval_seconds,
        health_refresh_timeout_seconds=health_refresh_timeout_seconds,
    )


def _default_target_types(kind: MCPServerKind) -> list[str]:
    """Conservative target hints for discovery metadata.

    Server-specific policy may narrow these later; these defaults never add
    an execution target and are used only to filter Agent candidates.
    """
    return default_target_types_for_kind(kind)


def _create_adapter(
    settings: MCPServerSettings,
    transport: MCPTransport,
    bundles: Mapping[str, McpProfileBundle] | None = None,
) -> MCPAdapter:
    if settings.allowed_tools is not None:
        transport = AllowlistedMCPTransport(transport, settings.allowed_tools)
    return create_adapter_for_settings(settings, transport, bundles)


def _create_transport(
    settings: MCPServerSettings,
) -> StreamableHTTPMCPTransport | StdioMCPTransport:
    if settings.transport == MCPTransportKind.STREAMABLE_HTTP:
        if settings.endpoint is None:
            raise MCPConfigurationError("Streamable HTTP MCP endpoint is missing")
        return StreamableHTTPMCPTransport(
            settings.endpoint,
            headers=settings.request_headers(),
        )
    if settings.transport == MCPTransportKind.STDIO:
        if settings.command is None:
            raise MCPConfigurationError("stdio MCP command is missing")
        return StdioMCPTransport(
            settings.command,
            args=settings.args,
            env=settings.process_environment,
        )
    raise MCPConfigurationError(f"unsupported MCP transport: {settings.transport}")
