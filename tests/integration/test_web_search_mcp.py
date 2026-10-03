"""Independent web-search MCP/Gateway checks; never contact Tavily."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import Client

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.servers.web_search import (
    ProviderFailure,
    ProviderSearchItem,
    ProviderSearchResponse,
    SearchConfig,
    SearchErrorCode,
    SearchResult,
)
from redteam.capability.servers.web_search_mcp import create_server, load_web_search_config
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.capability.web_search_manifest import validate_web_search_discovery
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from tests.integration.test_page_text_mcp import InProcessTransport

QUERY = 'OWASP API1:2023 "Broken Object Level Authorization"'
KEY = "synthetic-test-key"


class FakeProvider:
    provider_id = "tavily"

    def __init__(self, failure: SearchErrorCode | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.failure = failure

    async def search(self, query: str, max_results: int) -> ProviderSearchResponse:
        self.calls.append((query, max_results))
        if self.failure is not None:
            raise ProviderFailure(self.failure)
        return ProviderSearchResponse(items=(
            ProviderSearchItem(
                title="Synthetic reference", url="https://docs.example.org/reference",
                snippet="Synthetic\nsearch description",
            ),
        ))


def _config_file(path: Path, **updates: object) -> Path:
    path.write_text(json.dumps({"external_search_enabled": True, **updates}), encoding="utf-8")
    return path


def _settings(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    prefix = "REDTEAM_MCP_WEB_SEARCH"
    monkeypatch.setenv(f"{prefix}_KIND", "web_api")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", sys.executable)
    monkeypatch.setenv(f"{prefix}_RISK_LEVEL", "low")
    monkeypatch.setenv(f"{prefix}_PROFILE", "web_search_tavily")
    monkeypatch.setenv(f"{prefix}_SEARCH_CONFIG", str(path))
    monkeypatch.setenv("TAVILY_API_KEY", KEY)


def _invocation(
    *, query: str = QUERY, tool_id: str = "web.web_search", max_results: int = 5,
) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId("inv_web_search"),
        action_id=ActionId("action_web_search"),
        run_id=RunId("run_web_search"),
        tool_id=ToolId(tool_id),
        operation="web_search",
        arguments={"query": query, "max_results": max_results},
        timeout_seconds=5,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_web_search"), run_id=RunId("run_web_search")
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_inprocess_discovery_success_errors_and_disabled() -> None:
    config = SearchConfig(external_search_enabled=True, max_results=3, max_snippet_chars=12)
    fake = FakeProvider()
    server = create_server(config, provider=fake)
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert [tool.name for tool in tools] == ["web_search"]
        assert tools[0].input_schema["properties"]["max_results"]["maximum"] == 3
        assert set(tools[0].input_schema["properties"]) == {"query", "max_results"}
        assert tools[0].input_schema["additionalProperties"] is False
        assert tools[0].output_schema == SearchResult.model_json_schema()
        good = await client.call_tool("web_search", {"query": QUERY})
        assert not good.is_error
        assert good.structured_content["items"][0]["snippet"] == "Synthetic se"
        assert good.structured_content["truncated"] is True
        bad = await client.call_tool("web_search", {"query": "x\nsecret"})
        assert bad.is_error and "invalid_input" in str(bad.content[0])
        over = await client.call_tool("web_search", {"query": QUERY, "max_results": 4})
        assert over.is_error
        not_integer = await client.call_tool("web_search", {"query": QUERY, "max_results": "2"})
        assert not_integer.is_error is True
        boolean_count = await client.call_tool("web_search", {"query": QUERY, "max_results": True})
        assert boolean_count.is_error is True
        extra = await client.call_tool("web_search", {"query": QUERY, "api_key": KEY})
        assert extra.is_error and "invalid_input" in str(extra.content[0])
        sensitive = await client.call_tool("web_search", {"query": "Bearer secret-value"})
        assert sensitive.is_error and "sensitive_query" in str(sensitive.content[0])
        assert KEY not in str(good) + str(bad) + str(sensitive)
    assert fake.calls == [(QUERY, 3)]

    disabled = FakeProvider()
    async with Client(create_server(SearchConfig(), provider=disabled)) as client:
        refused = await client.call_tool("web_search", {"query": QUERY})
        assert refused.is_error and "external_search_disabled" in str(refused.content[0])
    assert disabled.calls == []

    failed = FakeProvider(SearchErrorCode.RATE_LIMITED)
    async with Client(create_server(config, provider=failed)) as client:
        result = await client.call_tool("web_search", {"query": QUERY})
        assert result.is_error and "rate_limited" in str(result.content[0])


async def test_gateway_registration_ids_schema_allowlist_and_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "search.json", max_results=2, max_output_bytes=1024)
    _settings(monkeypatch, path)
    settings = MCPServerSettings.from_environment("web-search")
    assert settings.allowed_tools == ("web_search",)
    assert settings.tool_operations == {"web_search": ("web_search",)}
    assert settings.process_environment == {"TAVILY_API_KEY": KEY}
    assert KEY not in repr(settings)
    config, _ = load_web_search_config(path, settings.web_search_config_sha256)
    fake = FakeProvider()
    transport = InProcessTransport(create_server(config, provider=fake))
    monkeypatch.setattr("redteam.capability.bootstrap._create_transport", lambda _: transport)
    gateway = await build_mcp_tool_gateway(["web-search"], health_refresh_enabled=False)
    descriptor = gateway.get(ToolId("web.web_search"))
    assert descriptor is not None and descriptor.output_schema is not None
    assert descriptor.operations == ["web_search"] and descriptor.target_types == []
    assert descriptor.input_schema["properties"]["max_results"]["maximum"] == 2
    altered = descriptor.model_copy(deep=True)
    altered.input_schema["properties"]["max_results"]["maximum"] = 10
    with pytest.raises(ValueError, match="discovery differs"):
        validate_web_search_discovery(altered, config)
    assert [tool.tool_id for tool in gateway.list_tools_for_action("web", "web_search")] == [
        ToolId("web.web_search")
    ]
    result = await gateway.invoke(_invocation(max_results=2))
    assert result.success and isinstance(result.output, dict)
    Draft202012Validator(descriptor.output_schema).validate(result.output)
    assert result.output["items"][0]["snippet"] == "Synthetic search description"
    assert result.action_id == ActionId("action_web_search")
    assert result.invocation_id == InvocationId("inv_web_search")
    assert result.tool_id == ToolId("web.web_search")
    assert KEY not in str(result) + str(descriptor)
    rejected = await gateway.invoke(_invocation(tool_id="web.not_allowed"))
    assert not rejected.success
    assert fake.calls == [(QUERY, 2)]
    wrong_output = InProcessTransport(create_server(config, provider=FakeProvider()),
                                      corrupt_output=True)
    monkeypatch.setattr("redteam.capability.bootstrap._create_transport", lambda _: wrong_output)
    mismatch_gateway = await build_mcp_tool_gateway(["web-search"], health_refresh_enabled=False)
    mismatch = await mismatch_gateway.invoke(_invocation(max_results=2))
    assert not mismatch.success
    assert mismatch.error is not None and mismatch.error.code == "tool_output_schema_mismatch"


async def test_actual_stdio_fake_provider_gateway_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "search.json")
    _settings(monkeypatch, path)
    settings = MCPServerSettings.from_environment("web-search")
    fixture = Path(__file__).with_name("_web_search_stdio_fake.py")

    def fake_stdio(_: MCPServerSettings) -> StdioMCPTransport:
        return StdioMCPTransport(
            sys.executable,
            args=(str(fixture), "--config", str(path), "--sha256",
                  settings.web_search_config_sha256 or ""),
            env={"TAVILY_API_KEY": KEY},
        )

    monkeypatch.setattr("redteam.capability.bootstrap._create_transport", fake_stdio)
    gateway = await build_mcp_tool_gateway(["web-search"], health_refresh_enabled=False)
    descriptor = gateway.get(ToolId("web.web_search"))
    assert descriptor is not None and descriptor.output_schema is not None
    result = await gateway.invoke(_invocation())
    assert result.success, result.error
    assert isinstance(result.output, dict)
    Draft202012Validator(descriptor.output_schema).validate(result.output)
    assert result.output["items"][0]["snippet"] == "Synthetic search description"
    assert result.action_id == ActionId("action_web_search")
    assert result.invocation_id == InvocationId("inv_web_search")
    assert result.tool_id == ToolId("web.web_search")
    assert KEY not in str(result)


async def test_actual_production_stdio_disabled_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "search.json", external_search_enabled=False)
    _settings(monkeypatch, path)
    monkeypatch.delenv("TAVILY_API_KEY")
    gateway = await build_mcp_tool_gateway(["web-search"], health_refresh_enabled=False)
    descriptor = gateway.get(ToolId("web.web_search"))
    assert descriptor is not None
    result = await gateway.invoke(_invocation())
    assert not result.success
    assert result.error is not None and result.error.code == "mcp_tool_error"
    assert "external_search_disabled" in str(result.output)
    assert result.action_id == ActionId("action_web_search")
    assert result.invocation_id == InvocationId("inv_web_search")


def test_search_profile_rejects_wrong_bindings_and_requires_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "search.json")
    _settings(monkeypatch, path)
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_ALLOWED_TOOLS_JSON", '["other"]')
    with pytest.raises(MCPConfigurationError, match="exact tool allowlist"):
        MCPServerSettings.from_environment("web-search")
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_ALLOWED_TOOLS_JSON", '["web_search"]')
    monkeypatch.setenv(
        "REDTEAM_MCP_WEB_SEARCH_TOOL_OPERATIONS_JSON", '{"web_search":["http_request"]}'
    )
    with pytest.raises(MCPConfigurationError, match="exact operation binding"):
        MCPServerSettings.from_environment("web-search")
    monkeypatch.delenv("REDTEAM_MCP_WEB_SEARCH_TOOL_OPERATIONS_JSON")
    monkeypatch.delenv("TAVILY_API_KEY")
    with pytest.raises(MCPConfigurationError, match="TAVILY_API_KEY is required"):
        MCPServerSettings.from_environment("web-search")


def test_search_config_is_pinned_and_disabled_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "search.json", external_search_enabled=False)
    _settings(monkeypatch, path)
    monkeypatch.delenv("TAVILY_API_KEY")
    settings = MCPServerSettings.from_environment("web-search")
    assert settings.process_environment == {}
    path.write_text('{"external_search_enabled":true}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_web_search_config(path, settings.web_search_config_sha256)
