"""HTTPX-backed MCP tools for explicitly scoped test accounts and endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server import MCPServer

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError, request


def create_server(policy: ServerPolicy) -> MCPServer:
    server = MCPServer("web-http", log_level="WARNING")
    execution_lock = asyncio.Lock()

    @server.tool()
    def session_list() -> dict[str, Any]:
        """List operator-provisioned test principals; never return credentials."""
        return {
            "sessions": [
                {
                    "session_ref": ref,
                    "target_ref": item.target_ref,
                    "principal_ref": item.principal_ref,
                }
                for ref, item in policy.sessions.items()
            ]
        }

    @server.tool()
    async def http_request(
        target_ref: str,
        path: str,
        method: str = "GET",
        session_ref: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one scoped request. No automatic redirects/retries; return body digest only.

        The operator enables methods and exact paths. A session_ref is bound to one target.
        This tool's success means execution completed, not that a vulnerability was found.
        """
        async with execution_lock:
            response = await request(
                policy,
                target_ref,
                path,
                method=method,
                session_ref=session_ref,
                json_body=json_body,
            )
        return {"target_ref": target_ref, "session_ref": session_ref, **response.summary()}

    @server.tool()
    async def http_compare_sessions(
        target_ref: str,
        path: str,
        first_session_ref: str,
        second_session_ref: str,
    ) -> dict[str, Any]:
        """Sequential GET with two distinct test principals; compare status/digests.

        Equality/difference is an observation, not proof of BOLA. No financial write requests.
        """
        first = policy.sessions.get(first_session_ref)
        second = policy.sessions.get(second_session_ref)
        if (
            first is None
            or second is None
            or first.target_ref != target_ref
            or second.target_ref != target_ref
            or first.principal_ref == second.principal_ref
        ):
            raise ToolInputError("two distinct principals bound to this target are required")
        async with execution_lock:
            a = await request(policy, target_ref, path, session_ref=first_session_ref)
            b = await request(policy, target_ref, path, session_ref=second_session_ref)
        return {
            "target_ref": target_ref,
            "first": {"principal_ref": first.principal_ref, **a.summary()},
            "second": {"principal_ref": second.principal_ref, **b.summary()},
            "same_status": a.status == b.status,
            "same_body": a.body == b.body,
            "verdict": "observation_only",
            "request_count": 2,
        }

    return server
