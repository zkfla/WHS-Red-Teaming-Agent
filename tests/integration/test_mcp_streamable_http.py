"""Real-protocol integration test for the harmless MCP execution substrate."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.app.bootstrap import build_application
from redteam.core.config import load_app_config
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from redteam.llm.fake import FakeLLMClient


def _unused_local_port() -> int:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
    except PermissionError:
        pytest.skip("sandbox does not permit binding a local integration-test port")


async def _wait_for_server(process: asyncio.subprocess.Process, port: int) -> None:
    for _ in range(100):
        if process.returncode is not None:
            output = await process.stdout.read() if process.stdout is not None else b""
            raise RuntimeError(f"MCP server exited early: {output.decode(errors='replace')}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError("MCP example server did not become ready")


async def test_real_streamable_http_discovery_and_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).parents[2]
    server_path = repository_root / "deploy/docker/mcp-example/server.py"
    port = _unused_local_port()
    environment = os.environ.copy()
    environment["MCP_EXAMPLE_PORT"] = str(port)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(server_path),
        cwd=repository_root,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        await _wait_for_server(process, port)
        monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_KIND", "web_api")
        monkeypatch.setenv(
            "REDTEAM_MCP_WEB_EXAMPLE_TOOL_OPERATIONS_JSON",
            '{"ping": ["service_discovery"]}',
        )
        monkeypatch.setenv(
            "REDTEAM_MCP_WEB_EXAMPLE_ENDPOINT", f"http://127.0.0.1:{port}/mcp"
        )
        config = load_app_config(repository_root / "examples/app.yaml")
        config.capability.mcp_servers = ["web-example"]
        _, deps = await build_application(config, llm=FakeLLMClient(), evidence_dir=tmp_path)
        run_id = RunId("run_mcp_integration")
        result = await deps.tool_gateway.invoke(
            ToolInvocation(
                invocation_id=InvocationId("inv_mcp_integration"),
                action_id=ActionId("action_mcp_integration"),
                run_id=run_id,
                tool_id=ToolId("web.ping"),
                operation="service_discovery",
                arguments={"message": "hello"},
                execution_context=ExecutionContext(
                    engagement_id=EngagementId("eng_mcp_integration"),
                    run_id=run_id,
                ),
                created_at=datetime.now(timezone.utc),
            )
        )

        assert result.success is True
        assert result.output == {"message": "hello", "status": "ok"}
        assert result.invocation_id == InvocationId("inv_mcp_integration")
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
