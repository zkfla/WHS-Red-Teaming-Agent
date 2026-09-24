"""Real-protocol integration for the external mobile MCP candidate (mobile-next).

Gated on ``npx`` being available (the server runs via
``npx -y @mobilenext/mobile-mcp@<pinned>`` over stdio). These tests exercise the
real Core Port -> Gateway -> adapter -> transport -> external MCP path and check
that the pinned manifest still matches live discovery. No device is required for
discovery/registration; a device-backed invoke is additionally gated on
``REDTEAM_TEST_DEVICE``.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

import pytest

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.mobile_analysis_manifest import (
    MOBILE_ANALYSIS_MANIFEST,
    MOBILE_NEXT_VERSION,
    SERVER_MOBILE_NEXT,
)
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId

_NPX = shutil.which("npx")
_PACKAGE = f"@mobilenext/mobile-mcp@{MOBILE_NEXT_VERSION}"
_MOBILE_NEXT_ARGS = ["-y", _PACKAGE]

pytestmark = pytest.mark.skipif(_NPX is None, reason="npx not available for mobile-next MCP")


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = "REDTEAM_MCP_MOBILE_NEXT"
    allow = list(MOBILE_ANALYSIS_MANIFEST.allowed_tools_for_server(SERVER_MOBILE_NEXT))
    bindings = MOBILE_ANALYSIS_MANIFEST.tool_operations_for_server(SERVER_MOBILE_NEXT)
    monkeypatch.setenv(f"{prefix}_KIND", "mobile")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", _NPX or "npx")
    monkeypatch.setenv(f"{prefix}_ARGS_JSON", json.dumps(_MOBILE_NEXT_ARGS))
    monkeypatch.setenv(f"{prefix}_ALLOWED_TOOLS_JSON", json.dumps(allow))
    monkeypatch.setenv(f"{prefix}_TOOL_OPERATIONS_JSON", json.dumps(bindings))


async def test_manifest_matches_live_mobile_next_discovery() -> None:
    transport = StdioMCPTransport(_NPX or "npx", args=_MOBILE_NEXT_ARGS)
    async with transport.session():
        tools = await transport.list_tools()
    # The pinned 4 candidates must still exist with the same argument contract.
    MOBILE_ANALYSIS_MANIFEST.validate_against_discovery(SERVER_MOBILE_NEXT, tools)


async def test_bootstrap_registers_only_allowlisted_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    gateway = await build_mcp_tool_gateway([SERVER_MOBILE_NEXT])

    launch = gateway.list_tools_for_action("mobile", "app_launch")
    assert {str(t.tool_id) for t in launch} == {"mobile.mobile_launch_app"}

    observation = gateway.list_tools_for_action("mobile", "response_analysis")
    assert {str(t.tool_id) for t in observation} == {
        "mobile.mobile_get_device_logs",
        "mobile.mobile_list_crashes",
        "mobile.mobile_get_crash",
    }


@pytest.mark.skipif(
    "REDTEAM_TEST_DEVICE" not in __import__("os").environ,
    reason="set REDTEAM_TEST_DEVICE=<adb serial> to run a device-backed invoke",
)
async def test_response_analysis_invoke_against_configured_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    device = os.environ["REDTEAM_TEST_DEVICE"]
    _env(monkeypatch)
    gateway = await build_mcp_tool_gateway([SERVER_MOBILE_NEXT])
    run_id = RunId("run_mobile_next")
    result = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_mobile_next"),
            action_id=ActionId("act_mobile_next"),
            run_id=run_id,
            tool_id=ToolId("mobile.mobile_list_crashes"),
            operation="response_analysis",
            arguments={"device": device},
            timeout_seconds=60.0,
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_mobile_next"), run_id=run_id
            ),
            created_at=datetime.now(timezone.utc),
        )
    )
    assert result.success is True
