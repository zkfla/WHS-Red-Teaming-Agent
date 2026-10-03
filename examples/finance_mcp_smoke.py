"""Operator-run connection test against the bundled synthetic lab, not an agent policy bypass.

Run the Compose smoke service inside finance-lab.yml's internal network.
All calls are bounded, directed to the supplied mock bank, and cannot transfer money.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
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
    posture = await call(
        "web.http_security_posture",
        "response_analysis",
        {"target_ref": "bank-lab", "path": "/api/accounts/alice", "session_ref": "alice"},
    )
    assert posture["cache_control_no_store"] and posture["content_type_nosniff"]
    assert posture["set_cookie_header_count"] == 2
    assert posture["cookies"][0]["http_only"] and posture["cookies"][0]["secure"]
    assert "synthetic-cookie" not in str(posture)
    anonymous = await call(
        "web.http_compare_anonymous",
        "response_analysis",
        {"target_ref": "bank-lab", "path": "/api/accounts/alice", "session_ref": "alice"},
    )
    assert anonymous["anonymous"]["status_code"] == 403
    assert anonymous["authenticated"]["status_code"] == 200
    assert anonymous["request_count"] == 2
    inventory = await call(
        "recon.html_inventory",
        "endpoint_discovery",
        {"target_ref": "bank-lab", "path": "/"},
    )
    assert inventory["references_followed"] is False
    assert any(item["path"] == "/assets/test-sdk.js" for item in inventory["references"])
    assert "synthetic-secret" not in str(inventory)
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
    assert "Synthetic Alice" in str(await call("web.browser_snapshot", "response_analysis", {}))
    await call("web.browser_run_code_unsafe", "runtime_instrumentation", {}, error=True)
    print(
        "PASS: five MCP servers; proxy replay; actor isolation; scope denial; browser continuity; "
        "security posture; anonymous comparison; HTML inventory"
    )


async def exercise_mobile_mcp(
    gateway: MCPToolGateway,
    server_id: str,
    config: dict,
    row: dict | None = None,
) -> dict:
    """Extend the same operator-run Gateway/Invocation smoke seam for the B/mobile stack."""
    from redteam.capability.mobile_analysis_manifest import MOBILE_ANALYSIS_TOOLS

    started = time.monotonic()
    row = row if row is not None else {"server": server_id}
    tools = [tool for tool in gateway.all() if tool.server_id == server_id]
    expected = {spec.tool_id for spec in MOBILE_ANALYSIS_TOOLS.get(server_id, ())}
    if expected and {str(tool.tool_id) for tool in tools} != expected:
        raise AssertionError("discovery differs from pinned manifest")
    assert tools, "empty discovery"
    row["discovery"] = "PASS"
    health = (await gateway.refresh_health(force=True))[server_id].value
    assert health == "healthy", f"health={health}"
    row["health"] = "PASS"
    index = 0
    calls = []
    row["calls"] = calls

    async def call(tool_id, arguments):
        nonlocal index
        index += 1
        descriptor = gateway.get(ToolId(tool_id))
        assert descriptor is not None and len(descriptor.operations) == 1
        run_id = RunId(config["run_id"])
        result = await gateway.invoke(
            ToolInvocation(
                invocation_id=InvocationId(f"mobile_smoke_{server_id}_{index}"),
                action_id=ActionId(f"mobile_smoke_{server_id}_{index}"),
                run_id=run_id,
                tool_id=ToolId(tool_id),
                operation=descriptor.operations[0],
                arguments=arguments,
                timeout_seconds=600,
                created_at=datetime.now(timezone.utc),
                execution_context=ExecutionContext(
                    engagement_id=EngagementId(config["engagement_id"]),
                    run_id=run_id,
                ),
            )
        )
        calls.append({"tool_id": tool_id, "success": result.success})
        assert result.success, result.error.code if result.error else "invoke failed"
        output = result.output
        if isinstance(output, str):
            try:
                return json.loads(output)
            except ValueError:
                return output
        return output

    apk_ref = config["apk_ref"]
    if server_id == "execution":
        output = await call("analysis.run_command", config["execution_arguments"])
        assert output["exit_code"] == 0, "execution returned nonzero exit code"
    elif server_id == "apktool":
        decoded = await call(
            "analysis.decode_apk",
            {
                "apk_path": config["apk_path"],
                "force": True,
                "timeout": 300,
            },
        )
        assert decoded.get("success") is True
        project = {"project_dir": decoded["output_dir"]}
        await call("analysis.get_manifest", project)
        await call("analysis.list_smali_files", {**project, "count": 5})
        await call(
            "analysis.search_in_files",
            {
                **project,
                "search_pattern": "password",
                "max_results": 5,
                "count": 5,
            },
        )
    elif server_id == "analysis-local":
        await call("analysis.apk_file_listing", {"apk_ref": apk_ref})
        await call(
            "analysis.apk_entry_digest", {"apk_ref": apk_ref, "entry_name": "AndroidManifest.xml"}
        )
        await call(
            "analysis.apk_string_search",
            {
                "apk_ref": apk_ref,
                "entry_name": "AndroidManifest.xml",
                "pattern": "android",
            },
        )
    elif server_id == "mobile-local":
        await call(
            "mobile.device_shell", {"device_ref": config["device_ref"], "command": "getprop"}
        )
    elif server_id == "mobile-next":
        device = {"device": config["device_ref"]}
        await call("mobile.mobile_launch_app", {**device, "packageName": config["package_name"]})
        await call(
            "mobile.mobile_get_device_logs",
            {
                **device,
                "limit": 20,
                "filter": [f"process={config['package_name']}"],
            },
        )
        crash_skip = config.get("mobile_crash_skip")
        if crash_skip:
            for name in ("mobile_list_crashes", "mobile_get_crash"):
                calls.append({"tool_id": f"mobile.{name}", "skip": crash_skip})
        else:
            crashes = await call("mobile.mobile_list_crashes", device)
            entries = crashes.get("crashes", []) if isinstance(crashes, dict) else crashes
            assert isinstance(entries, list), "crash inventory has an unexpected shape"
            app_entries = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and config["package_name"] in entry.get("processName", "")
            ]
            if app_entries:
                await call("mobile.mobile_get_crash", {**device, "id": app_entries[0]["id"]})
            else:
                calls.append(
                    {
                        "tool_id": "mobile.mobile_get_crash",
                        "skip": "no observed approved-app crash ID",
                    }
                )
    elif server_id == "jadx":
        await call("analysis.get_android_manifest", {})
    elif server_id == "mobsf":
        uploaded = await call(
            "analysis.mobsf_submit_apk",
            {
                "apk_ref": apk_ref,
                "expected_sha256": config["sha256"],
            },
        )
        owned = {"apk_ref": apk_ref, "scan_ref": uploaded["scan_ref"]}
        await call("analysis.mobsf_start_scan", owned)
        await call("analysis.mobsf_scan_status", owned)
        report = await call(
            "analysis.mobsf_report_summary",
            {
                **owned,
                "sections": ["manifest", "permission", "secret", "network"],
            },
        )
        assert report["status"] == "completed"
        Path(config["report_path"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    else:
        raise AssertionError("unrecognized smoke server")
    row.update(
        {
            "server": server_id,
            "discovery": "PASS",
            "health": "PASS",
            "invoke": "PASS",
            "calls": calls,
            "seconds": round(time.monotonic() - started, 2),
            "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    return row


async def mobile_mcp_main(config_path: str, servers: list[str] | None = None) -> None:
    """One independent session per server; failures remain visible in the result table."""
    import os

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    for name, value in config.get("environment", {}).items():
        os.environ[name] = value
    order = (
        "execution",
        "apktool",
        "mobile-next",
        "jadx",
        "analysis-local",
        "mobile-local",
        "mobsf",
    )
    result_path = Path(config["result_path"])
    rows = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if servers and result_path.exists()
        else []
    )
    for server in servers or order:
        rows = [row for row in rows if row["server"] != server]
        row = {"server": server, "discovery": "NOT_RUN", "health": "NOT_RUN", "invoke": "NOT_RUN"}
        reason = config.get("skip", {}).get(server)
        if reason:
            assert server not in {"apktool", "mobsf"}, "Docker static-analysis servers cannot skip"
            rows.append(
                {
                    "server": server,
                    "discovery": "SKIP",
                    "health": "SKIP",
                    "invoke": "SKIP",
                    "reason": reason,
                }
            )
            continue
        try:
            async with open_mcp_tool_gateway([server]) as gateway:
                await exercise_mobile_mcp(gateway, server, config, row)
            rows.append(row)
        except Exception as exc:
            stage = next(
                (name for name in ("discovery", "health", "invoke") if row[name] != "PASS"),
                "invoke",
            )
            row[stage] = "FAIL"
            row["reason"] = type(exc).__name__
            rows.append(row)
            Path(config["result_path"]).write_text(json.dumps(rows, indent=2), encoding="utf-8")
            raise
        result_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    rows.sort(key=lambda row: order.index(row["server"]))
    Path(config["result_path"]).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mobile-mcp-config")
    parser.add_argument(
        "--mobile-mcp-server",
        action="append",
        choices=[
            "execution",
            "apktool",
            "mobile-next",
            "jadx",
            "analysis-local",
            "mobile-local",
            "mobsf",
        ],
    )
    options = parser.parse_args()
    asyncio.run(
        mobile_mcp_main(options.mobile_mcp_config, options.mobile_mcp_server)
        if options.mobile_mcp_config
        else main()
    )
