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
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.settings import MCPConfigurationError, mcp_server_ids_from_environment


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tools", help="Inspect the Capability Tool Registry")
    tools_sub = parser.add_subparsers(dest="tools_command", required=True)

    list_parser = tools_sub.add_parser("list", help="List every registered tool")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=run_list)

    health_parser = tools_sub.add_parser("health", help="Show per-server health")
    health_parser.add_argument("--json", action="store_true")
    health_parser.set_defaults(handler=run_health)


async def _build_gateway():  # noqa: ANN202
    # Same source `redteam run` uses: REDTEAM_MCP_SERVERS, not an automatic
    # empty/Example default.
    server_ids = mcp_server_ids_from_environment()
    if not server_ids:
        # Same reality `redteam run` sees for the same configuration (empty
        # catalog, no adapters) -- never the in-process ExampleMCPAdapter,
        # which would show a tool here that `run` could never actually use.
        eprint("WARNING: no MCP servers configured -- the tool catalog is empty.")
        return MCPToolGateway(MCPToolRegistry(), {})
    if server_ids == [EXAMPLE_MCP_SENTINEL]:
        # Same explicit, deliberate opt-in `build_application` recognizes.
        return await build_mcp_tool_gateway([])
    return await build_mcp_tool_gateway(server_ids)


async def run_list(args: argparse.Namespace) -> int:
    try:
        gateway = await _build_gateway()
    except (MCPConfigurationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_FAILURE

    descriptors = gateway.all()
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
        gateway = await _build_gateway()
    except (MCPConfigurationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_FAILURE

    by_server: dict[str, set[str]] = {}
    for descriptor in gateway.all():
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
