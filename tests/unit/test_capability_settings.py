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
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_MAX_OUTPUT_BYTES", "262144")
    monkeypatch.setenv("REDTEAM_MCP_WEB_EXAMPLE_TOKEN_ENV_VAR", "TEST_MCP_TOKEN")
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-value")

    settings = MCPServerSettings.from_environment("web-example")

    assert settings.kind == MCPServerKind.WEB_API
    assert settings.transport == MCPTransportKind.STREAMABLE_HTTP
    assert settings.risk_level == RiskLevel.MEDIUM
    assert settings.max_concurrency == 3
    assert settings.max_output_bytes == 262144
    assert settings.request_headers() == {"Authorization": "Bearer secret-value"}
    assert "secret-value" not in repr(settings)


def test_server_settings_load_exact_authorization_value_by_indirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_ZAP_ENDPOINT", "http://127.0.0.1:8282")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_AUTHORIZATION_ENV_VAR", "ZAP_MCP_KEY")
    monkeypatch.setenv("ZAP_MCP_KEY", "raw-zap-security-key")

    settings = MCPServerSettings.from_environment("zap")

    assert settings.request_headers() == {"Authorization": "raw-zap-security-key"}
    assert "raw-zap-security-key" not in repr(settings)


def test_bearer_and_exact_authorization_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_ZAP_ENDPOINT", "http://127.0.0.1:8282")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_TOKEN_ENV_VAR", "BEARER_SECRET")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_AUTHORIZATION_ENV_VAR", "ZAP_MCP_KEY")
    monkeypatch.setenv("BEARER_SECRET", "bearer-secret")
    monkeypatch.setenv("ZAP_MCP_KEY", "raw-key")

    with pytest.raises(MCPConfigurationError, match="mutually exclusive"):
        MCPServerSettings.from_environment("zap")


def test_exact_authorization_rejects_header_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_ZAP_ENDPOINT", "http://127.0.0.1:8282")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_ZAP_AUTHORIZATION_ENV_VAR", "ZAP_MCP_KEY")
    monkeypatch.setenv("ZAP_MCP_KEY", "key\r\nX-Injected: true")

    with pytest.raises(MCPConfigurationError, match="line breaks"):
        MCPServerSettings.from_environment("zap")


def test_server_settings_load_canonical_tool_operation_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "http://localhost:8765/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")
    monkeypatch.setenv(
        "REDTEAM_MCP_WEB_ALLOWED_TOOLS_JSON",
        '["http_request","operator_status"]',
    )
    monkeypatch.setenv(
        "REDTEAM_MCP_WEB_TOOL_OPERATIONS_JSON",
        '{"http_request":["http_request","response_analysis"],"operator_status":[]}',
    )

    settings = MCPServerSettings.from_environment("web")

    assert settings.tool_operations == {
        "http_request": ("http_request", "response_analysis"),
        "operator_status": (),
    }


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


@pytest.mark.parametrize("value", ["0", "33", "1.5", "many"])
def test_invalid_server_concurrency_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "http://localhost:8765/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_MAX_CONCURRENCY", value)

    with pytest.raises(MCPConfigurationError, match="MAX_CONCURRENCY"):
        MCPServerSettings.from_environment("web")


@pytest.mark.parametrize("value", ["0", "1023", "16777217", "1.5", "many"])
def test_invalid_server_output_limit_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "http://localhost:8765/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_MAX_OUTPUT_BYTES", value)

    with pytest.raises(MCPConfigurationError, match="MAX_OUTPUT_BYTES"):
        MCPServerSettings.from_environment("web")


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
            tool_id=ToolId("recon.ping"),
            operation="service_discovery",
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


@pytest.mark.parametrize("value", ["[]", "{}", "[null]", '["a","a"]', "not-json"])
def test_invalid_tool_allowlist_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REDTEAM_MCP_BROWSER_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_BROWSER_ENDPOINT", "http://localhost:8931/mcp")
    monkeypatch.setenv("REDTEAM_MCP_BROWSER_ALLOWED_TOOLS_JSON", value)
    with pytest.raises(MCPConfigurationError, match="ALLOWED_TOOLS_JSON"):
        MCPServerSettings.from_environment("browser")


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "{}",
        '{"request":"http_request"}',
        '{"request":["HTTP_REQUEST"]}',
        '{"request":["http_request","http_request"]}',
        "not-json",
    ],
)
def test_invalid_tool_operation_bindings_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "http://localhost:8931/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_TOOL_OPERATIONS_JSON", value)
    with pytest.raises(MCPConfigurationError, match="TOOL_OPERATIONS_JSON"):
        MCPServerSettings.from_environment("web")


def test_tool_operation_binding_keys_cannot_escape_tool_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "http://localhost:8931/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ALLOWED_TOOLS_JSON", '["http_request"]')
    monkeypatch.setenv(
        "REDTEAM_MCP_WEB_TOOL_OPERATIONS_JSON",
        '{"different_tool":["http_request"]}',
    )
    with pytest.raises(MCPConfigurationError, match="keys must be a subset"):
        MCPServerSettings.from_environment("web")


def test_tool_allowlist_accepts_partial_operation_bindings(monkeypatch) -> None:
    monkeypatch.setenv("REDTEAM_MCP_WEB_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ENDPOINT", "http://localhost:8931/mcp")
    monkeypatch.setenv("REDTEAM_MCP_WEB_ALLOWED_TOOLS_JSON", '["http_request", "new_reader"]')
    monkeypatch.setenv("REDTEAM_MCP_WEB_TOOL_OPERATIONS_JSON", '{"http_request":["http_request"]}')
    settings = MCPServerSettings.from_environment("web")
    assert settings.allowed_tools == ("http_request", "new_reader")
    assert settings.tool_operations == {"http_request": ("http_request",)}
