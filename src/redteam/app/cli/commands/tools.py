"""``redteam tools list`` / ``redteam tools health`` — read the live Registry.

Mirrors the same tool-gateway resolution ``app.bootstrap.build_application``
uses for ``redteam run`` -- no separate health subsystem, no separate
catalog model, and no separate answer to "what MCP servers are active."
An empty ``capability.mcp_servers`` is not an error: it is an honest empty
catalog (surfaced as a warning), never a silent ExampleMCPAdapter
substitute. ``capability.mcp_servers == ["example"]`` is the one explicit,
deliberate way to see that in-process test fixture here too (see
``app.bootstrap.EXAMPLE_MCP_SENTINEL``).
"""

from __future__ import annotations

import argparse

from redteam.app.bootstrap import EXAMPLE_MCP_SENTINEL
from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, eprint, print_json, render_table
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.gated_manifest import build_gated_descriptors, validate_gated_descriptors
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.settings import (
    MCPConfigurationError,
    gated_network_policy_path_from_environment,
    mcp_server_ids_from_environment,
    shell_policy_path_from_environment,
)
from redteam.capability.shell_assembly import build_gated_provider_config
from redteam.capability.shell_manifest import SHELL_SERVER_ID
from redteam.capability.shell_runtime import build_shell_runtime


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tools", help="Inspect the Capability Tool Registry")
    tools_sub = parser.add_subparsers(dest="tools_command", required=True)

    list_parser = tools_sub.add_parser("list", help="List every registered tool")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=run_list)

    health_parser = tools_sub.add_parser("health", help="Show per-server health")
    health_parser.add_argument("--json", action="store_true")
    health_parser.set_defaults(handler=run_health)


def _offline_descriptors(gateway, *, shell_runtime, gated_provider_config):
    """Offline display catalog: gateway rows plus validated gated manifest rows.

    Display/catalog metadata only for `list`/`health`: appending here never
    activates the gated provider (no window, no ledger file, no Docker) and
    never makes a gated tool invocable through this gateway -- the adapter
    still fails closed without `set_gated_policy`, which only the run/resume
    composition root calls. Gated rows carry the discovered shell-server
    health so the list never overclaims liveness from static metadata.
    """
    descriptors = list(gateway.all())
    if gated_provider_config is None or shell_runtime is None:
        return descriptors
    gated = build_gated_descriptors(shell_runtime.policy)
    validate_gated_descriptors(gated, shell_runtime.policy)
    statuses = {item.health_status for item in descriptors if item.server_id == SHELL_SERVER_ID}
    if len(statuses) == 1:
        (only,) = statuses
        gated = [item.model_copy(update={"health_status": only}) for item in gated]
    return [*descriptors, *gated]


async def _build_gateway():  # noqa: ANN202
    # Same source `redteam run` uses: REDTEAM_MCP_SERVERS, not an automatic
    # empty/Example default.
    server_ids = mcp_server_ids_from_environment()
    # Same shell assembly as `redteam run`: None means disabled (honest catalog
    # without shell tools); an invalid policy raises MCPConfigurationError.
    shell_runtime = build_shell_runtime(shell_policy_path_from_environment())
    # Same gated source as `redteam run`: None means closed. A present policy
    # is validated here (invalid raises MCPConfigurationError); a gated
    # policy without the shell provider is a config error, mirroring run.
    gated_provider_config = build_gated_provider_config(
        gated_network_policy_path_from_environment()
    )
    if gated_provider_config is not None and shell_runtime is None:
        raise MCPConfigurationError(
            "gated network policy requires the shell provider: set "
            "REDTEAM_SHELL_POLICY_PATH"
        )
    builtin_adapters = [shell_runtime.adapter] if shell_runtime is not None else []
    builtin_concurrency = dict(shell_runtime.concurrency) if shell_runtime is not None else None
    builtin_output_limits = dict(shell_runtime.output_limits) if shell_runtime is not None else None
    if not server_ids and not builtin_adapters:
        # Same reality `redteam run` sees for the same configuration (empty
        # catalog, no adapters) -- never the in-process ExampleMCPAdapter,
        # which would show a tool here that `run` could never actually use.
        eprint("WARNING: no MCP servers configured -- the tool catalog is empty.")
        return MCPToolGateway(MCPToolRegistry(), {}), []
    if server_ids == [EXAMPLE_MCP_SENTINEL]:
        # Same explicit, deliberate opt-in `build_application` recognizes.
        if builtin_adapters:
            from redteam.capability.adapters.mcp.example import ExampleMCPAdapter

            gateway = await build_mcp_tool_gateway(
                [],
                builtin_adapters=[ExampleMCPAdapter(), *builtin_adapters],
                builtin_concurrency=builtin_concurrency,
                builtin_output_limits=builtin_output_limits,
            )
        else:
            gateway = await build_mcp_tool_gateway([])
    else:
        gateway = await build_mcp_tool_gateway(
            server_ids,
            builtin_adapters=builtin_adapters,
            builtin_concurrency=builtin_concurrency,
            builtin_output_limits=builtin_output_limits,
        )
    return (
        gateway,
        _offline_descriptors(
            gateway, shell_runtime=shell_runtime, gated_provider_config=gated_provider_config
        ),
    )


async def run_list(args: argparse.Namespace) -> int:
    try:
        _gateway, descriptors = await _build_gateway()
    except (MCPConfigurationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_FAILURE
    if args.json:
        print_json([d.model_dump(mode="json") for d in descriptors])
        return EXIT_OK

    rows = [
        [
            str(d.tool_id),
            ", ".join(d.capability_tags) or "-",
            d.risk_level.value.upper(),
            d.health_status.value.upper(),
        ]
        for d in sorted(descriptors, key=lambda d: str(d.tool_id))
    ]
    render_table(["TOOL ID", "CAPABILITY", "RISK", "HEALTH"], rows)
    return EXIT_OK


async def run_health(args: argparse.Namespace) -> int:
    try:
        _gateway, descriptors = await _build_gateway()
    except (MCPConfigurationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_FAILURE

    by_server: dict[str, set[str]] = {}
    for descriptor in descriptors:
        by_server.setdefault(descriptor.server_id, set()).add(descriptor.health_status.value)

    if args.json:
        print_json(
            {
                server_id: ("HEALTHY" if statuses == {"healthy"} else "DEGRADED")
                for server_id, statuses in by_server.items()
            }
        )
        return EXIT_OK

    if not by_server:
        print("(no MCP servers registered)")
        return EXIT_OK
    for server_id, statuses in sorted(by_server.items()):
        overall = "HEALTHY" if statuses == {"healthy"} else "DEGRADED"
        print(f"{server_id:<20} {overall}")
    return EXIT_OK
