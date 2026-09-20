"""``redteam tools list`` / ``redteam tools health`` — read the live Registry.

Builds the same `MCPToolGateway` the canonical bootstrap
(`app.bootstrap.build_application`) would, via
`capability.bootstrap.build_mcp_tool_gateway` -- no separate health
subsystem, no separate catalog model. An empty `capability.mcp_servers`
list is not an error: it is the documented in-process-example path (see
`ExampleMCPAdapter`), surfaced here as an explicit warning rather than a
silent difference from `run`.
"""

from __future__ import annotations

import argparse

from redteam.app.cli._shared import (
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
    print_json,
    render_table,
)
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.settings import MCPConfigurationError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tools", help="Inspect the Capability Tool Registry")
    tools_sub = parser.add_subparsers(dest="tools_command", required=True)

    list_parser = tools_sub.add_parser("list", help="List every registered tool")
    list_parser.add_argument("--config", dest="config_path", default=None)
    list_parser.add_argument("engagement_path", nargs="?", default=None)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=run_list)

    health_parser = tools_sub.add_parser("health", help="Show per-server health")
    health_parser.add_argument("--config", dest="config_path", default=None)
    health_parser.add_argument("engagement_path", nargs="?", default=None)
    health_parser.add_argument("--json", action="store_true")
    health_parser.set_defaults(handler=run_health)


async def _build_gateway(args: argparse.Namespace):  # noqa: ANN202
    server_ids = _configured_server_ids(args)
    if not server_ids:
        eprint(
            "WARNING: no MCP servers configured -- using the in-process "
            "ExampleMCPAdapter (development/test only; no real tool execution)."
        )
    return await build_mcp_tool_gateway(server_ids)


def _configured_server_ids(args: argparse.Namespace) -> list[str]:
    if args.config_path is None:
        return []
    if args.engagement_path is None:
        raise ValueError("--config requires an engagement_path to build a full AppConfig")
    config = load_app_config_for_run(args.engagement_path, args.config_path)
    return list(config.capability.mcp_servers)


async def run_list(args: argparse.Namespace) -> int:
    try:
        gateway = await _build_gateway(args)
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
        gateway = await _build_gateway(args)
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
