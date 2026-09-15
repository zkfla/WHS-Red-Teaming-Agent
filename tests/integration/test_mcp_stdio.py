"""Real-protocol integration test for local MCP stdio bootstrap."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


@pytest.mark.parametrize(
    ("server_id", "kind", "tool_id"),
    [
        ("web-local", "web_api", ToolId("web.ping")),
        ("mobile-local", "mobile", ToolId("mobile.ping")),
        ("recon-local", "recon", ToolId("recon.ping")),
        ("analysis-local", "analysis", ToolId("analysis.ping")),
    ],
)
async def test_all_adapter_kinds_support_real_stdio_discovery_and_invocation(
    monkeypatch: pytest.MonkeyPatch,
    server_id: str,
    kind: str,
    tool_id: ToolId,
) -> None:
    repository_root = Path(__file__).parents[2]
    server_path = repository_root / "deploy/docker/mcp-example/server.py"
    prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
    monkeypatch.setenv(f"{prefix}_KIND", kind)
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", sys.executable)
    monkeypatch.setenv(f"{prefix}_ARGS_JSON", json.dumps([str(server_path)]))
    monkeypatch.setenv(f"{prefix}_PASSTHROUGH_ENV", "MCP_EXAMPLE_TRANSPORT")
    monkeypatch.setenv("MCP_EXAMPLE_TRANSPORT", "stdio")

    gateway = await build_mcp_tool_gateway([server_id])
    run_id = RunId("run_mcp_stdio")
    result = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_mcp_stdio"),
            action_id=ActionId("action_mcp_stdio"),
            run_id=run_id,
            tool_id=tool_id,
            operation="ping",
            arguments={"message": "stdio-ready"},
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_mcp_stdio"),
                run_id=run_id,
            ),
            created_at=datetime.now(timezone.utc),
        )
    )

    assert result.success is True
    assert result.output == {"message": "stdio-ready", "status": "ok"}
