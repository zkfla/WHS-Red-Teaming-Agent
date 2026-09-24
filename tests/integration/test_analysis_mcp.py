"""Real-protocol integration for the external analysis MCP candidate (apktool).

Gated on a local checkout of ``zinja-coder/apktool-mcp-server`` (pointed to by
``REDTEAM_TEST_APKTOOL_MCP_DIR``) plus ``uv`` and the ``apktool`` binary. The
decode test additionally needs a real APK at ``REDTEAM_TEST_APK`` (e.g. the
InsecureBankv2 learning app). These tests drive the real Core Port -> Gateway ->
adapter -> transport -> external MCP path, decode an APK, and read its manifest.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.mobile_analysis_manifest import (
    MOBILE_ANALYSIS_MANIFEST,
    SERVER_ANALYSIS_APKTOOL,
)
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId

_UV = shutil.which("uv")
_APKTOOL = shutil.which("apktool")
_SERVER_DIR = os.environ.get("REDTEAM_TEST_APKTOOL_MCP_DIR")


def _server_args() -> list[str]:
    return ["run", "--directory", _SERVER_DIR or "", "apktool_mcp_server.py"]


_READY = bool(_UV and _APKTOOL and _SERVER_DIR and Path(_SERVER_DIR or "").is_dir())
pytestmark = pytest.mark.skipif(
    not _READY,
    reason="need uv, apktool, and REDTEAM_TEST_APKTOOL_MCP_DIR for apktool-mcp",
)


def _env(monkeypatch: pytest.MonkeyPatch, workspace: str) -> None:
    prefix = "REDTEAM_MCP_ANALYSIS_APKTOOL"
    allow = list(MOBILE_ANALYSIS_MANIFEST.allowed_tools_for_server(SERVER_ANALYSIS_APKTOOL))
    bindings = MOBILE_ANALYSIS_MANIFEST.tool_operations_for_server(SERVER_ANALYSIS_APKTOOL)
    monkeypatch.setenv(f"{prefix}_KIND", "analysis")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", _UV or "uv")
    monkeypatch.setenv(f"{prefix}_ARGS_JSON", json.dumps(_server_args()))
    monkeypatch.setenv(f"{prefix}_ALLOWED_TOOLS_JSON", json.dumps(allow))
    monkeypatch.setenv(f"{prefix}_TOOL_OPERATIONS_JSON", json.dumps(bindings))
    monkeypatch.setenv("APKTOOL_WORKSPACE", workspace)
    monkeypatch.setenv(f"{prefix}_PASSTHROUGH_ENV", "APKTOOL_WORKSPACE")


async def test_manifest_matches_live_apktool_discovery() -> None:
    transport = StdioMCPTransport(_UV or "uv", args=_server_args())
    async with transport.session():
        tools = await transport.list_tools()
    MOBILE_ANALYSIS_MANIFEST.validate_against_discovery(SERVER_ANALYSIS_APKTOOL, tools)


async def test_bootstrap_registers_nine_static_analysis_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, str(tmp_path / "ws"))
    gateway = await build_mcp_tool_gateway([SERVER_ANALYSIS_APKTOOL])
    candidates = gateway.list_tools_for_action("analysis", "static_analysis")
    assert len(candidates) == 9
    assert "analysis.decode_apk" in {str(t.tool_id) for t in candidates}


@pytest.mark.skipif(
    "REDTEAM_TEST_APK" not in os.environ,
    reason="set REDTEAM_TEST_APK=<path to a learning APK> to run the decode test",
)
async def test_decode_apk_and_read_manifest_through_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    apk_path = os.environ["REDTEAM_TEST_APK"]
    workspace = tmp_path / "ws"
    _env(monkeypatch, str(workspace))
    gateway = await build_mcp_tool_gateway([SERVER_ANALYSIS_APKTOOL])
    run_id = RunId("run_apktool")
    ctx = ExecutionContext(engagement_id=EngagementId("eng_apktool"), run_id=run_id)

    decode = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_decode"),
            action_id=ActionId("act_decode"),
            run_id=run_id,
            tool_id=ToolId("analysis.decode_apk"),
            operation="static_analysis",
            arguments={"apk_path": apk_path},
            timeout_seconds=180.0,
            execution_context=ctx,
            created_at=datetime.now(timezone.utc),
        )
    )
    assert decode.success is True, decode.error
    assert isinstance(decode.output, dict)
    project_dir = decode.output.get("output_dir")
    assert project_dir, decode.output

    manifest = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_manifest"),
            action_id=ActionId("act_manifest"),
            run_id=run_id,
            tool_id=ToolId("analysis.get_manifest"),
            operation="static_analysis",
            arguments={"project_dir": project_dir},
            timeout_seconds=60.0,
            execution_context=ctx,
            created_at=datetime.now(timezone.utc),
        )
    )
    assert manifest.success is True, manifest.error
    assert isinstance(manifest.output, dict)
    assert "package=" in manifest.output.get("manifest", "")
