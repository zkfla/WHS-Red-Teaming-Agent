"""Capability endpoint configuration and bootstrap tests."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.settings import (
    MCPConfigurationError,
    MCPServerKind,
    MCPServerSettings,
    MCPTransportKind,
    environment_prefix,
)
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.enums import RiskLevel
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


def test_server_settings_load_endpoint_kind_risk_and_secret_by_indirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_ENDPOINT", "http://127.0.0.1:8765/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_RISK_LEVEL", "medium")
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_TOKEN_ENV_VAR", "TEST_MCP_TOKEN")
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-value")

    settings = MCPServerSettings.from_environment("web-example")

    assert settings.kind == MCPServerKind.WEB_API
    assert settings.transport == MCPTransportKind.STREAMABLE_HTTP
    assert settings.risk_level == RiskLevel.MEDIUM
    assert settings.request_headers() == {"Authorization": "Bearer secret-value"}
    assert "secret-value" not in repr(settings)


def test_remote_plain_http_requires_explicit_lab_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_RECON_ENDPOINT", "http://recon.internal/mcp")
    monkeypatch.setenv("REDTEAM_MCP_RECON_KIND", "recon")

    with pytest.raises(MCPConfigurationError, match="must use HTTPS"):
        MCPServerSettings.from_environment("recon")

    monkeypatch.setenv("REDTEAM_MCP_RECON_ALLOW_INSECURE_HTTP", "true")
    assert MCPServerSettings.from_environment("recon").kind == MCPServerKind.RECON


def test_invalid_server_id_cannot_be_mapped_to_environment() -> None:
    with pytest.raises(MCPConfigurationError):
        environment_prefix("../../secret")


def test_endpoint_credentials_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "https://username:password@mcp.example/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")

    with pytest.raises(MCPConfigurationError, match="must not contain credentials"):
        MCPServerSettings.from_environment("web")


def test_stdio_settings_load_argv_and_only_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_LOCAL_KIND", "mobile")
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_LOCAL_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_LOCAL_COMMAND", sys.executable)
    monkeypatch.setenv(
        "REDTEAM_MCP_MOBILE_LOCAL_ARGS_JSON",
        json.dumps(["server.py", "--device", "emulator-5554"]),
    )
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_LOCAL_PASSTHROUGH_ENV", "ANDROID_SERIAL, TEST_SECRET")
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5554")
    monkeypatch.setenv("TEST_SECRET", "not-visible-in-repr")

    settings = MCPServerSettings.from_environment("mobile-local")

    assert settings.kind == MCPServerKind.MOBILE
    assert settings.transport == MCPTransportKind.STDIO
    assert settings.endpoint is None
    assert settings.command == sys.executable
    assert settings.args == ("server.py", "--device", "emulator-5554")
    assert settings.process_environment == {
        "ANDROID_SERIAL": "emulator-5554",
        "TEST_SECRET": "not-visible-in-repr",
    }
    assert "not-visible-in-repr" not in repr(settings)


def test_stdio_arguments_must_be_json_string_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_ANALYSIS_KIND", "analysis")
    monkeypatch.setenv("REDTEAM_MCP_ANALYSIS_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_ANALYSIS_COMMAND", "analysis-server")
    monkeypatch.setenv("REDTEAM_MCP_ANALYSIS_ARGS_JSON", '{"not": "an array"}')

    with pytest.raises(MCPConfigurationError, match="JSON array of strings"):
        MCPServerSettings.from_environment("analysis")


async def test_empty_server_config_preserves_example_gateway() -> None:
    gateway = await build_mcp_tool_gateway([])
    run_id = RunId("run_default_gateway")
    result = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv_default_gateway"),
            action_id=ActionId("action_default_gateway"),
            run_id=run_id,
            tool_id=ToolId("example.ping"),
            operation="ping",
            arguments={"message": "ready"},
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_default_gateway"),
                run_id=run_id,
            ),
            created_at=datetime.now(timezone.utc),
        )
    )

    assert result.success is True


async def test_duplicate_server_ids_fail_before_discovery() -> None:
    with pytest.raises(MCPConfigurationError, match="duplicates"):
        await build_mcp_tool_gateway(["web", "web"])
