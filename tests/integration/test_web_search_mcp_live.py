"""Explicit one-call Tavily check through the real stdio MCP Gateway."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.servers.web_search import SearchResult
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


async def test_tavily_mcp_gateway_live_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("REDTEAM_TAVILY_MCP_LIVE") != "1":
        pytest.skip("live MCP check requires REDTEAM_TAVILY_MCP_LIVE=1")
    if not os.environ.get("TAVILY_API_KEY"):
        pytest.skip("live MCP check requires TAVILY_API_KEY")

    policy = tmp_path / "search-policy.json"
    policy.write_text(json.dumps({"external_search_enabled": True}), encoding="utf-8")
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_COMMAND", sys.executable)
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_PROFILE", "web_search_tavily")
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_SEARCH_CONFIG", str(policy))
    monkeypatch.setenv("REDTEAM_MCP_WEB_SEARCH_RISK_LEVEL", "low")

    invocation = ToolInvocation(
        invocation_id=InvocationId("inv_tavily_mcp_live"),
        action_id=ActionId("action_tavily_mcp_live"),
        run_id=RunId("run_tavily_mcp_live"),
        tool_id=ToolId("web.web_search"),
        operation="web_search",
        arguments={
            "query": 'OWASP API1:2023 "Broken Object Level Authorization"',
            "max_results": 5,
        },
        timeout_seconds=15,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_tavily_mcp_live"),
            run_id=RunId("run_tavily_mcp_live"),
        ),
        created_at=datetime.now(timezone.utc),
    )
    async with open_mcp_tool_gateway(["web-search"], health_refresh_enabled=False) as gateway:
        descriptor = gateway.get(ToolId("web.web_search"))
        assert descriptor is not None and descriptor.output_schema is not None
        result = await gateway.invoke(invocation)
        assert result.success, result.error
        assert isinstance(result.output, dict)
        Draft202012Validator(descriptor.output_schema).validate(result.output)
        assert result.action_id == invocation.action_id
        assert result.invocation_id == invocation.invocation_id
        assert result.tool_id == invocation.tool_id
        assert result.output["provider"] == "tavily"
        assert len(result.output["items"]) <= 5
        assert len(SearchResult.model_validate(result.output).model_dump_json().encode()) <= 65536
