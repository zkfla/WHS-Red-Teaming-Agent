"""Discover operator-installed external Web MCPs without touching a target.

Usage example::

    REDTEAM_EXTERNAL_MCP_SERVERS=zap-recon,zap-web \
      ./.venv/bin/python examples/external_mcp_discovery.py

The matching ``REDTEAM_MCP_<SERVER>_*`` variables must already be configured.
This command only starts the MCP transport, lists tools, and prints redacted
Registry metadata. It does not call a target, start a scan, or log secrets.
"""

from __future__ import annotations

import asyncio
import os

from redteam.capability.bootstrap import open_mcp_tool_gateway


def server_ids() -> list[str]:
    raw = os.environ.get("REDTEAM_EXTERNAL_MCP_SERVERS", "")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise SystemExit("REDTEAM_EXTERNAL_MCP_SERVERS is required")
    if len(values) != len(set(values)):
        raise SystemExit("REDTEAM_EXTERNAL_MCP_SERVERS contains duplicates")
    return values


async def main() -> None:
    ids = server_ids()
    async with open_mcp_tool_gateway(ids) as gateway:
        for descriptor in gateway.all():
            print(
                f"tool_id={descriptor.tool_id} server={descriptor.server_id} "
                f"health={descriptor.health_status.value} risk={descriptor.risk_level.value} "
                f"operations={descriptor.operations} "
                f"required={descriptor.required_arguments} "
                f"optional={descriptor.optional_arguments}"
            )


if __name__ == "__main__":
    asyncio.run(main())
