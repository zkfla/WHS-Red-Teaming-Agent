"""Real MCP-wire tests for the analysis fallback server.

The zip-level tools here have no external binary dependency, so the full
success path runs unconditionally over a real spawned MCP stdio subprocess
and through the full settings -> transport -> adapter -> gateway bootstrap.

A separate, skipped group exercises the real candidate external MCP server
(apktool-mcp-server) once the operator has it checked out and ``uv``
installed — see ``REDTEAM_TEST_APKTOOL_MCP_DIR`` below. That candidate is
not vendored into this repository and is not finalized (see
``deploy/mcp/README.md`` "Analysis capability"), so it is skipped by
default rather than assumed present.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId

_MODULE_ARGS = ["-m", "redteam.capability.servers.analysis"]
_STATIC_ANALYSIS_TOOLS = {"apk_file_listing", "apk_entry_digest", "apk_string_search"}


@pytest.fixture
def apk_path(tmp_path: Path) -> Path:
    path = tmp_path / "sample.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"binary-manifest-placeholder")
        archive.writestr("res/values/strings.xml", b"<resources>lab-only-marker</resources>")
    return path


def _write_policy(tmp_path: Path, apk_path: Path) -> Path:
    policy_path = tmp_path / "analysis-policy.json"
    policy_path.write_text(
        json.dumps({"apks": {"sample": {"path": str(apk_path)}}}), encoding="utf-8"
    )
    return policy_path


async def test_real_stdio_process_discovers_all_static_analysis_tools(
    tmp_path: Path, apk_path: Path
) -> None:
    policy_path = _write_policy(tmp_path, apk_path)
    transport = StdioMCPTransport(
        sys.executable, args=[*_MODULE_ARGS, "--policy", str(policy_path)]
    )
    tools = await transport.list_tools()
    assert {tool.name for tool in tools} == _STATIC_ANALYSIS_TOOLS


async def test_real_stdio_process_lists_and_searches_registered_apk(
    tmp_path: Path, apk_path: Path
) -> None:
    policy_path = _write_policy(tmp_path, apk_path)
    transport = StdioMCPTransport(
        sys.executable, args=[*_MODULE_ARGS, "--policy", str(policy_path)]
    )

    listing = await transport.call_tool("apk_file_listing", {"apk_ref": "sample"})
    assert listing.is_error is False
    assert listing.output is not None
    names = {entry["name"] for entry in listing.output["entries"]}
    assert names == {"AndroidManifest.xml", "res/values/strings.xml"}

    search = await transport.call_tool(
        "apk_string_search",
        {
            "apk_ref": "sample",
            "entry_name": "res/values/strings.xml",
            "pattern": "lab-only-marker",
        },
    )
    assert search.is_error is False
    assert search.output is not None
    assert search.output["match_count"] == 1


async def test_static_analysis_tools_bound_to_canonical_operation_through_full_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, apk_path: Path
) -> None:
    """Exercises the settings -> transport -> adapter -> gateway path the
    real ``apktool`` server ID would use, with our fallback server standing
    in as the wire endpoint (see deploy/mcp/README.md)."""
    policy_path = _write_policy(tmp_path, apk_path)
    server_id = "analysis-fallback-test"
    prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
    monkeypatch.setenv(f"{prefix}_KIND", "analysis")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", sys.executable)
    monkeypatch.setenv(
        f"{prefix}_ARGS_JSON", json.dumps([*_MODULE_ARGS, "--policy", str(policy_path)])
    )
    monkeypatch.setenv(
        f"{prefix}_TOOL_OPERATIONS_JSON",
        json.dumps(
            {
                "apk_file_listing": ["static_analysis"],
                "apk_entry_digest": ["static_analysis"],
                "apk_string_search": ["static_analysis"],
            }
        ),
    )

    gateway = await build_mcp_tool_gateway([server_id])
    run_id = RunId("run_analysis_fallback")
    result = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_analysis_fallback"),
            action_id=ActionId("action_analysis_fallback"),
            run_id=run_id,
            tool_id=ToolId("analysis.apk_file_listing"),
            operation="static_analysis",
            arguments={"apk_ref": "sample"},
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_analysis_fallback"),
                run_id=run_id,
            ),
            created_at=datetime.now(timezone.utc),
        )
    )
    assert result.success is True
    assert result.output is not None
    assert {entry["name"] for entry in result.output["entries"]} == {
        "AndroidManifest.xml",
        "res/values/strings.xml",
    }


_APKTOOL_MCP_DIR = os.environ.get("REDTEAM_TEST_APKTOOL_MCP_DIR")


@pytest.mark.skipif(
    shutil.which("uv") is None or not _APKTOOL_MCP_DIR,
    reason=(
        "requires the uv package manager on PATH and "
        "REDTEAM_TEST_APKTOOL_MCP_DIR pointing at a local apktool-mcp-server "
        "checkout with apktool installed (candidate, not vendored here — "
        "see deploy/mcp/README.md 'Analysis capability')"
    ),
)
async def test_real_apktool_mcp_server_discovery() -> None:
    transport = StdioMCPTransport(
        "uv", args=["--directory", str(_APKTOOL_MCP_DIR), "run", "apktool_mcp_server.py"]
    )
    tools = await transport.list_tools()
    names = {tool.name for tool in tools}
    assert "decode_apk" in names
    assert "get_manifest" in names
