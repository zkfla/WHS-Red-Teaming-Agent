"""Operator-run connection test against the bundled synthetic lab, not an agent policy bypass.

Run the Compose smoke service inside finance-lab.yml's internal network.
All calls are bounded, directed to the supplied mock bank, and cannot transfer money.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.gateway import MCPToolGateway
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


async def main() -> None:
    async with open_mcp_tool_gateway(
        [
            "web-http",
            "web-proxy",
            "web-browser",
            "recon-passive",
            "recon-active",
        ]
    ) as gateway:
        await exercise(gateway)


async def exercise(gateway: MCPToolGateway) -> None:
    run_id = RunId("finance_smoke")
    index = 0

    async def call(
        tool_id: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        error: bool = False,
    ) -> Any:
        nonlocal index
        index += 1
        result = await gateway.invoke(
            ToolInvocation(
                invocation_id=InvocationId(f"smoke_{index}"),
                action_id=ActionId(f"smoke_{index}"),
                run_id=run_id,
                tool_id=ToolId(tool_id),
                operation=operation,
                arguments=arguments,
                timeout_seconds=45,
                execution_context=ExecutionContext(
                    engagement_id=EngagementId("lab"), run_id=run_id
                ),
                created_at=datetime.now(timezone.utc),
            )
        )
        assert result.success != error, f"{tool_id} returned unexpected success={result.success}"
        return result.output

    response = await call(
        "web.http_request",
        "http_request",
        {
            "target_ref": "bank-lab",
            "path": "/api/accounts/alice",
            "session_ref": "alice",
        },
    )
    assert response["status_code"] == 200 and response["body_omitted"]
    comparison = await call(
        "web.http_compare_sessions",
        "response_analysis",
        {
            "target_ref": "bank-lab",
            "path": "/api/accounts/alice",
            "first_session_ref": "alice",
            "second_session_ref": "bob",
        },
    )
    assert comparison["first"]["status_code"] == 200
    assert comparison["second"]["status_code"] == 403
    await call(
        "web.http_request",
        "http_request",
        {"target_ref": "bank-lab", "path": "/transfer"},
        error=True,
    )
    await call(
        "web.http_request",
        "http_request",
        {
            "target_ref": "bank-lab",
            "path": "/api/accounts/alice",
            "method": "POST",
        },
        error=True,
    )
    proxy_status = await call("web.proxy_status", "response_analysis", {})
    assert proxy_status["provider"] == "har-v1" and proxy_status["flow_count"] == 2
    assert proxy_status["ignored_out_of_scope_flows"] == 1
    flows = (
        await call(
            "web.proxy_list_flows",
            "response_analysis",
            {"target_ref": "bank-lab"},
        )
    )["flows"]
    read_flow = next(flow for flow in flows if flow["method"] == "GET")
    write_flow = next(flow for flow in flows if flow["method"] == "POST")
    detail = await call(
        "web.proxy_get_flow",
        "response_analysis",
        {"flow_ref": read_flow["flow_ref"]},
    )
    assert "discarded-capture-token" not in str(detail)
    replay = await call(
        "web.proxy_replay_flow",
        "http_request",
        {"flow_ref": read_flow["flow_ref"], "session_ref": "alice"},
    )
    assert replay["status_code"] == 200 and replay["body_omitted"]
    proxy_comparison = await call(
        "web.proxy_compare_sessions",
        "response_analysis",
        {
            "flow_ref": read_flow["flow_ref"],
            "first_session_ref": "alice",
            "second_session_ref": "bob",
        },
    )
    assert proxy_comparison["first"]["status_code"] == 200
    assert proxy_comparison["second"]["status_code"] == 403
    mutation = await call(
        "web.proxy_mutate_json",
        "parameter_fuzz",
        {
            "flow_ref": write_flow["flow_ref"],
            "session_ref": "alice",
            "mutations": {"amount": 100001},
        },
    )
    assert mutation["status_code"] == 200 and mutation["mutated_fields"] == ["amount"], mutation
    await call(
        "web.proxy_mutate_json",
        "parameter_fuzz",
        {"flow_ref": write_flow["flow_ref"], "mutations": {"admin": True}},
        error=True,
    )
    for name, operation, arguments in (
        ("dns_resolve", "service_discovery", {"target_ref": "bank-lab"}),
        ("http_probe", "service_discovery", {"target_ref": "bank-lab", "path": "/health"}),
        (
            "openapi_inventory",
            "endpoint_discovery",
            {"target_ref": "bank-lab", "path": "/openapi.json"},
        ),
        (
            "oidc_inventory",
            "endpoint_discovery",
            {"target_ref": "bank-lab", "path": "/.well-known/openid-configuration"},
        ),
    ):
        await call(f"recon.{name}", operation, arguments)
    await call("recon.port_scan", "port_scan", {"target_ref": "bank-lab"}, error=True)
    await call(
        "web.browser_navigate",
        "http_request",
        {"url": "http://mock-bank:8080"},
    )
    snapshot = await call("web.browser_snapshot", "response_analysis", {})
    assert "Synthetic bank" in str(snapshot), "browser state lost between invocations"
    await call(
        "web.browser_fill_form",
        "auth_attempt",
        {
            "fields": [
                {
                    "target": "#alias",
                    "name": "Test alias",
                    "type": "textbox",
                    "value": "Synthetic Alice",
                },
            ]
        },
    )
    await call("web.browser_click", "auth_attempt", {"target": "#confirm"})
    assert "Synthetic Alice" in str(
        await call("web.browser_snapshot", "response_analysis", {})
    )
    await call("web.browser_run_code_unsafe", "runtime_instrumentation", {}, error=True)
    print("PASS: five MCP servers; proxy replay; actor isolation; scope denial; browser continuity")


if __name__ == "__main__":
    asyncio.run(main())
