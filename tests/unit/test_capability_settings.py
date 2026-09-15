"""Capability endpoint configuration and bootstrap tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.settings import (
    MCPConfigurationError,
    MCPServerKind,
    MCPServerSettings,
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
    monkeypatch.setenv(
        "REDTEAM_MCP_WEB_ENDPOINT", "https://username:password@mcp.example/mcp"
    )
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")

    with pytest.raises(MCPConfigurationError, match="must not contain credentials"):
        MCPServerSettings.from_environment("web")


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
