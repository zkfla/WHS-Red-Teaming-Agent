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
            "web-browser",
            "recon-passive",
            "recon-active",
        ]
    ) as gateway:
        await exercise(gateway)


async def exercise(gateway: MCPToolGateway) -> None:
    run_id = RunId("finance_smoke")
    index = 0

    async def call(tool_id: str, arguments: dict[str, Any], *, error: bool = False) -> Any:
        nonlocal index
        index += 1
        result = await gateway.invoke(
            ToolInvocation(
                invocation_id=InvocationId(f"smoke_{index}"),
                action_id=ActionId(f"smoke_{index}"),
                run_id=run_id,
                tool_id=ToolId(tool_id),
                operation=tool_id.split(".", 1)[1],
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
        {
            "target_ref": "bank-lab",
            "path": "/api/accounts/alice",
            "session_ref": "alice",
        },
    )
    assert response["status_code"] == 200 and response["body_omitted"]
    comparison = await call(
        "web.http_compare_sessions",
        {
            "target_ref": "bank-lab",
            "path": "/api/accounts/alice",
            "first_session_ref": "alice",
            "second_session_ref": "bob",
        },
    )
    assert comparison["first"]["status_code"] == 200
    assert comparison["second"]["status_code"] == 403
    await call("web.http_request", {"target_ref": "bank-lab", "path": "/transfer"}, error=True)
    await call(
        "web.http_request",
        {
            "target_ref": "bank-lab",
            "path": "/api/accounts/alice",
            "method": "POST",
        },
        error=True,
    )
    for name, arguments in (
        ("dns_resolve", {"target_ref": "bank-lab"}),
        ("http_probe", {"target_ref": "bank-lab", "path": "/health"}),
        ("openapi_inventory", {"target_ref": "bank-lab", "path": "/openapi.json"}),
        ("oidc_inventory", {"target_ref": "bank-lab", "path": "/.well-known/openid-configuration"}),
    ):
        await call(f"recon.{name}", arguments)
    assert not (await call("recon.active_status", {}))["enabled"]
    await call("recon.port_scan", {"target_ref": "bank-lab"}, error=True)
    await call("web.browser_navigate", {"url": "http://mock-bank:8080"})
    snapshot = await call("web.browser_snapshot", {})
    assert "Synthetic bank" in str(snapshot), "browser state lost between invocations"
    await call("web.browser_fill_form", {"fields": [
        {"target": "#alias", "name": "Test alias", "type": "textbox", "value": "Synthetic Alice"},
    ]})
    await call("web.browser_click", {"target": "#confirm"})
    assert "Synthetic Alice" in str(await call("web.browser_snapshot", {}))
    await call("web.browser_run_code_unsafe", {}, error=True)
    await call("web.browser_close", {})
    print(
        "PASS: all four MCP servers; actor isolation; scope/method/tool denial; browser continuity"
    )


if __name__ == "__main__":
    asyncio.run(main())
