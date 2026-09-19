"""Read live ZAP capture pages through the existing neutral HAR boundary."""

import asyncio
import os
import re
from typing import Any

import anyio
from mcp.server import MCPServer

from redteam.capability.connectors.proxy_capture import _validate_zap_origin, fetch_zap_har
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError
from redteam.capability.servers.proxy import HarCaptureBackend


def create_server(policy: ServerPolicy, api_origin: str, api_key_env: str) -> MCPServer:
    origin = _validate_zap_origin(api_origin)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ToolInputError("invalid ZAP credential environment name")
    server = MCPServer("web-zap", log_level="WARNING")
    lock = asyncio.Lock()

    @server.tool()
    async def zap_capture_snapshot(target_ref: str) -> dict[str, Any]:
        """Read one current ZAP HAR page; no target requests, scan, spider or config changes.

        Requires the ZAP Import/Export add-on. Returns scoped summaries, not credentials/bodies.
        Each call refreshes the snapshot. flow_ref belongs to this snapshot only.
        """
        target = policy.targets.get(target_ref)
        if target is None:
            raise ToolInputError("unknown target_ref")
        key = os.environ.get(api_key_env)
        if not key or any(c in key for c in "\r\n\x00"):
            raise ToolInputError("ZAP API credential unavailable")
        try:
            with anyio.fail_after(policy.timeout_seconds):
                async with lock:
                    raw = await fetch_zap_har(
                        origin, target.origin, key, policy.proxy_max_flows,
                        timeout_seconds=policy.timeout_seconds,
                    )
        except TimeoutError as exc:
            raise ToolInputError("ZAP capture request timed out") from exc
        backend = HarCaptureBackend.from_bytes(raw, policy)
        flows = backend.list_flows(target_ref)
        return {
            "target_ref": target_ref,
            "provider": "zap-live-har",
            "flows": [flow.summary() for flow in flows],
            "page_limit": policy.proxy_max_flows,
            "coverage": "first_page_only",
            "target_request_count": 0,
            "zap_api_request_count": 1,
            "source_untrusted": True,
        }

    return server
