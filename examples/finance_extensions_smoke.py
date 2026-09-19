"""Operator-run, synthetic integration check with real ZAP and Nuclei containers."""

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


async def main() -> None:
    # Seed real ZAP history with one explicit synthetic GET, no spider/active scan.
    async with httpx.AsyncClient(proxy="http://zap-engine:8090", trust_env=False) as client:
        response = await client.get("http://mock-bank:8080/health")
        assert response.status_code == 200
    async with open_mcp_tool_gateway(["web-http", "web-zap", "web-nuclei"]) as gateway:
        index = 0

        async def call(tool: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
            nonlocal index
            index += 1
            result = await gateway.invoke(ToolInvocation(
                invocation_id=InvocationId(f"extension_{index}"),
                action_id=ActionId(f"extension_{index}"), run_id=RunId("extensions"),
                tool_id=ToolId(tool), operation=operation, arguments=arguments,
                timeout_seconds=45,
                execution_context=ExecutionContext(
                    engagement_id=EngagementId("lab"), run_id=RunId("extensions"),
                ),
                created_at=datetime.now(timezone.utc),
            ))
            assert result.success, f"{tool}: {result.error}"
            assert isinstance(result.output, dict)
            return result.output

        output = await call("web.http_request", "http_request", {
            "target_ref": "bank-lab", "path": "/health", "query": {"probe": "extension"},
            "request_headers": {"X-Test-Case": "synthetic"},
        })
        assert output["status_code"] == 200
        snapshot = await call("web.zap_capture_snapshot", "response_analysis", {
            "target_ref": "bank-lab",
        })
        assert any(flow["path"] == "/health" for flow in snapshot["flows"])
        for check, expected in (("missing-csp", True), ("missing-nosniff-header", False)):
            match = await call("web.nuclei_response_check", "response_analysis", {
                "target_ref": "bank-lab", "path": "/health", "check_id": check,
            })
            assert match["matched"] is expected, match
        print(
            "PASS: scoped query/headers; live ZAP capture; real Nuclei positive/negative matching"
        )


if __name__ == "__main__":
    asyncio.run(main())
