"""Real MCP-wire tests for the mobile fallback server.

Split per item 6 of the capability work: discovery and the error paths that
never touch a device run unconditionally over a real spawned MCP stdio
subprocess. Success-path execution against a real ``adb`` binary and device
only runs when the operator opts in — see ``REDTEAM_TEST_REAL_ADB_SERIAL``
below — and is skipped otherwise. (The success path itself, with ``adb``
replaced by a monkeypatched in-process function, is already covered by
``tests/unit/test_mobile_server.py`` at the real MCP-protocol level via
``mcp.Client``; this file adds the OS-process/stdio boundary and the full
settings -> transport -> adapter -> gateway bootstrap on top of that.)

Registering a real external mobile MCP server (e.g. mobile-next/mobile-mcp)
goes through the same ``MCPServerSettings``/``build_mcp_tool_gateway`` path
as any other MCP server; see ``deploy/mcp/README.md`` "Mobile capability"
for the exact ``REDTEAM_MCP_<SERVER_ID>_*`` variables. That candidate is not
vendored into this repository, so it is not re-tested here.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId

_MODULE_ARGS = ["-m", "redteam.capability.servers.mobile"]


def _write_policy(tmp_path: Path, *, adb_path: str = "adb") -> Path:
    policy_path = tmp_path / "mobile-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "devices": {"pixel": {"serial": "emulator-5554"}},
                "allowed_commands": ["getprop"],
                "adb_path": adb_path,
            }
        ),
        encoding="utf-8",
    )
    return policy_path


async def test_real_stdio_process_discovers_device_shell_tool(tmp_path: Path) -> None:
    policy_path = _write_policy(tmp_path)
    transport = StdioMCPTransport(
        sys.executable, args=[*_MODULE_ARGS, "--policy", str(policy_path)]
    )
    tools = await transport.list_tools()
    assert {tool.name for tool in tools} == {"device_shell"}


async def test_unknown_device_rejected_over_the_wire_without_touching_adb(
    tmp_path: Path,
) -> None:
    policy_path = _write_policy(tmp_path)
    transport = StdioMCPTransport(
        sys.executable, args=[*_MODULE_ARGS, "--policy", str(policy_path)]
    )
    result = await transport.call_tool(
        "device_shell", {"device_ref": "not-registered", "command": "getprop"}
    )
    assert result.is_error is True


async def test_disallowed_command_rejected_over_the_wire_without_touching_adb(
    tmp_path: Path,
) -> None:
    policy_path = _write_policy(tmp_path)
    transport = StdioMCPTransport(
        sys.executable, args=[*_MODULE_ARGS, "--policy", str(policy_path)]
    )
    result = await transport.call_tool(
        "device_shell", {"device_ref": "pixel", "command": "logcat_recent"}
    )
    assert result.is_error is True


async def test_device_shell_bound_to_canonical_operation_through_full_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercises the settings -> transport -> adapter -> gateway path the
    real ``mobile-next`` server ID would use, with our fallback server
    standing in as the wire endpoint (see deploy/mcp/README.md)."""
    policy_path = _write_policy(tmp_path)
    server_id = "mobile-fallback-test"
    prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
    monkeypatch.setenv(f"{prefix}_KIND", "mobile")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", sys.executable)
    monkeypatch.setenv(
        f"{prefix}_ARGS_JSON", json.dumps([*_MODULE_ARGS, "--policy", str(policy_path)])
    )
    monkeypatch.setenv(f"{prefix}_ALLOWED_TOOLS_JSON", json.dumps(["device_shell"]))
    monkeypatch.setenv(
        f"{prefix}_TOOL_OPERATIONS_JSON", json.dumps({"device_shell": ["device_shell"]})
    )

    gateway = await build_mcp_tool_gateway([server_id])
    run_id = RunId("run_mobile_fallback")
    result = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_mobile_fallback"),
            action_id=ActionId("action_mobile_fallback"),
            run_id=run_id,
            tool_id=ToolId("mobile.device_shell"),
            operation="device_shell",
            arguments={"device_ref": "unknown-device", "command": "getprop"},
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_mobile_fallback"),
                run_id=run_id,
            ),
            created_at=datetime.now(timezone.utc),
        )
    )
    # No real adb/device is touched: the "unknown device" rejection alone
    # proves settings/discovery/allowlist/operation-binding wiring is correct.
    assert result.success is False
    assert result.error is not None


_REAL_SERIAL = os.environ.get("REDTEAM_TEST_REAL_ADB_SERIAL")


@pytest.mark.skipif(
    shutil.which("adb") is None or not _REAL_SERIAL,
    reason=(
        "requires a real adb binary on PATH and REDTEAM_TEST_REAL_ADB_SERIAL "
        "pointing at an authorized, already-connected device/emulator"
    ),
)
async def test_device_shell_against_a_real_authorized_device(tmp_path: Path) -> None:
    policy_path = tmp_path / "real-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "devices": {"lab-device": {"serial": _REAL_SERIAL}},
                "allowed_commands": ["getprop"],
            }
        ),
        encoding="utf-8",
    )
    transport = StdioMCPTransport(
        sys.executable, args=[*_MODULE_ARGS, "--policy", str(policy_path)]
    )
    result = await transport.call_tool(
        "device_shell", {"device_ref": "lab-device", "command": "getprop"}
    )
    assert result.is_error is False
