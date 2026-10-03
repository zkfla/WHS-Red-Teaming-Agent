"""MobSF boundary tests: real SDK schemas, HTTP allowlist, scope and output handling."""

from __future__ import annotations

import asyncio
import hashlib
import time
import zipfile
from datetime import datetime, timezone

import httpx
import pytest
from mcp import Client

from redteam.capability.adapters.mcp.mobsf import MobSFMCPAdapter
from redteam.capability.mobile_analysis_manifest import (
    MOBILE_ANALYSIS_TOOLS,
    validate_discovered_schema,
)
from redteam.capability.servers.mobsf import (
    MobSFClient,
    MobSFError,
    MobSFPolicy,
    create_server,
    normalize_report,
)
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ExecutionContext, ToolDescriptor, ToolInvocation, ToolResult
from redteam.core.enums import ToolHealthStatus
from redteam.orchestration.mobile_mcp_binding import MobileMCPEligibility
from redteam.orchestration.observations.mobsf import map_mobsf_report
from tests.integration._engine_llm_harness import FakeCatalog

IMAGE = "opensecurity/mobile-security-framework-mobsf:v4.4.6@sha256:" + "a" * 64


@pytest.fixture
def policy(tmp_path):
    apk = tmp_path / "approved.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
    key = tmp_path / "key"
    key.write_text("secret-api-key")
    return MobSFPolicy(
        enabled=True,
        endpoint="http://127.0.0.1:18000",
        api_key_file=str(key),
        image=IMAGE,
        workspace_root=str(tmp_path),
        analysis={"apks": {"apk": {"path": str(apk)}}},
        hashes={"apk": hashlib.sha256(apk.read_bytes()).hexdigest()},
        engagement_id="eng",
        run_id="run",
        state_path=str(tmp_path / "state.sqlite3"),
        poll_interval_seconds=0.01,
        max_polls=2,
    )


def client_for(policy, *, scan_error=False, missing_report=False):
    calls = []
    data = open(policy.analysis.apks["apk"].path, "rb").read()
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()

    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "secret-api-key"
        route = request.url.path.rpartition("/")[2]
        if request.method == "GET":
            return httpx.Response(405)
        if route == "upload":
            return httpx.Response(200, json={"hash": md5, "scan_type": "apk"})
        if route == "scan":
            return httpx.Response(500 if scan_error else 200, json={"hash": md5})
        if route == "report_json":
            return httpx.Response(
                404 if missing_report else 200,
                json={
                    "md5": md5,
                    "manifest_analysis": {
                        "manifest_findings": [
                            {
                        "rule": "app_is_debuggable",
                                "severity": "high",
                                "description": "secret-api-key",
                            },
                        ]
                    },
                    "permissions": {"android.permission.SEND_SMS": {}},
                    "secrets": ["credential-value"],
                    "network_security": {"network_findings": []},
                },
            )
        raise AssertionError("unexpected endpoint")

    return MobSFClient(policy, transport=httpx.MockTransport(respond)), calls


async def test_owned_upload_scan_status_report_and_restart(policy):
    client, calls = client_for(policy)
    assert await client.healthy()
    uploaded = await client.submit("apk", policy.hashes["apk"], "eng", "run")
    assert uploaded.status == "uploaded"
    assert (await client.start("apk", uploaded.scan_ref, "eng", "run")).status == "completed"
    restarted, _ = client_for(policy)
    assert (await restarted.status("apk", uploaded.scan_ref, "eng", "run")).status == "completed"
    report = await restarted.summary(
        "apk", uploaded.scan_ref, ["manifest", "permission", "secret", "network"], "eng", "run"
    )
    serialized = report.model_dump_json()
    assert "credential-value" not in serialized and "secret-api-key" not in serialized
    assert report.sections["secret"]["candidates"][0]["value"] == "[REDACTED]"
    assert {route for _, route in calls} <= {
        "/api/v1/upload",
        "/api/v1/scan",
        "/api/v1/report_json",
    }
    for other in [("other", "run"), ("eng", "other")]:
        with pytest.raises(MobSFError, match="scope_denied"):
            await restarted.status("apk", uploaded.scan_ref, *other)
    with restarted._db() as db:
        db.execute("UPDATE scans SET expires=?", (time.time() - 1,))
    with pytest.raises(MobSFError, match="scope_denied"):
        await restarted.status("apk", uploaded.scan_ref, "eng", "run")


@pytest.mark.parametrize(
    "ref,digest,eng,run",
    [
        ("/etc/passwd", "a" * 64, "eng", "run"),
        ("../apk", "a" * 64, "eng", "run"),
        ("apk", "a" * 64, "eng", "run"),
        ("apk", "a" * 64, "other", "run"),
        ("apk", "a" * 64, "eng", "other"),
    ],
)
async def test_invalid_asset_hash_context_never_uploads(policy, ref, digest, eng, run):
    client, calls = client_for(policy)
    with pytest.raises(MobSFError):
        await client.submit(ref, digest, eng, run)
    assert calls == []


async def test_registered_file_tampering_never_uploads(policy):
    client, calls = client_for(policy)
    from pathlib import Path

    Path(policy.analysis.apks["apk"].path).write_bytes(b"tampered")
    with pytest.raises(MobSFError, match="hash_mismatch"):
        await client.submit("apk", policy.hashes["apk"], "eng", "run")
    assert calls == []


async def test_allowlist_disabled_key_and_poll_limit(policy):
    client, calls = client_for(policy, missing_report=True)
    with pytest.raises(MobSFError, match="route_denied"):
        await client._request("unsupported")
    assert not await MobSFClient(policy.model_copy(update={"enabled": False})).healthy()
    assert not await MobSFClient(policy.model_copy(update={"api_key_file": "missing"})).healthy()
    uploaded = await client.submit("apk", policy.hashes["apk"], "eng", "run")
    with pytest.raises(MobSFError, match="poll_limit"):
        await client.start("apk", uploaded.scan_ref, "eng", "run")
    assert (await client.status("apk", uploaded.scan_ref, "eng", "run")).status == "timeout"
    assert sum(path.endswith("report_json") for _, path in calls) == policy.max_polls


async def test_scan_failure_and_cancel_are_distinct(policy):
    client, _ = client_for(policy, scan_error=True)
    uploaded = await client.submit("apk", policy.hashes["apk"], "eng", "run")
    with pytest.raises(MobSFError, match="scan_failed"):
        await client.start("apk", uploaded.scan_ref, "eng", "run")
    assert (await client.status("apk", uploaded.scan_ref, "eng", "run")).status == "failed"
    uploaded = await client.submit("apk", policy.hashes["apk"], "eng", "run")
    original = client._request

    async def cancel(route, **kwargs):
        if route == "scan":
            raise asyncio.CancelledError()
        return await original(route, **kwargs)

    client._request = cancel
    with pytest.raises(asyncio.CancelledError):
        await client.start("apk", uploaded.scan_ref, "eng", "run")
    assert (await client.status("apk", uploaded.scan_ref, "eng", "run")).status == "cancelled"


async def test_output_caps_schema_drift_and_section_allowlist(policy):
    client, _ = client_for(policy)
    uploaded = await client.submit("apk", policy.hashes["apk"], "eng", "run")
    await client.start("apk", uploaded.scan_ref, "eng", "run")
    with pytest.raises(MobSFError, match="section_denied"):
        await client.summary("apk", uploaded.scan_ref, ["source"], "eng", "run")
    oversized = MobSFClient(
        policy,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (policy.max_response_bytes + 1))
        ),
    )
    with pytest.raises(MobSFError, match="response_too_large"):
        await oversized._request("report_json")
    with pytest.raises(MobSFError, match="schema_drift"):
        normalize_report({"permissions": []}, ["permission"])
    assert normalize_report({}, ["manifest"])[1] == ["manifest"]


async def test_actual_mcp_discovery_matches_pinned_schema(policy):
    async with Client(create_server(policy)) as sdk:
        listing = await sdk.list_tools()
    specs = {spec.tool_id.rpartition(".")[2]: spec for spec in MOBILE_ANALYSIS_TOOLS["mobsf"]}
    assert {tool.name for tool in listing.tools} == set(specs)
    for tool in listing.tools:
        validate_discovered_schema(specs[tool.name], tool.input_schema, tool.output_schema)


@pytest.mark.parametrize(
    "health,visible",
    [
        (ToolHealthStatus.HEALTHY, False),
        (ToolHealthStatus.UNKNOWN, False),
        (ToolHealthStatus.UNHEALTHY, True),
    ],
)
@pytest.mark.parametrize(
    "fallback,primary,capability",
    [
        ("analysis-local", "apktool", "analysis"),
        ("mobile-local", "mobile-next", "mobile"),
    ],
)
def test_fallback_health_gate(health, visible, fallback, primary, capability):
    descriptors = [
        ToolDescriptor(
            tool_id=f"{capability}.{server}",
            server_id=server,
            tool_name=server,
            capability_tags=[capability],
            operations=["static_analysis"],
            health_status=status,
        )
        for server, status in [(fallback, ToolHealthStatus.HEALTHY), (primary, health)]
    ]
    catalog = FakeCatalog(descriptors)
    gate = MobileMCPEligibility(catalog, catalog, None)
    assert bool(gate.get(descriptors[0].tool_id)) == visible
    assert gate.get(descriptors[1].tool_id) == descriptors[1]


def engagement(enabled=False):
    return Engagement(
        engagement_id="eng",
        name="MobSF test",
        scope=EngagementScope(allowed_assets=["apk"]),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata={"mobsf_agent": {"enabled": enabled, "assets": {"apk": "a" * 64}}},
    )


@pytest.mark.parametrize(
    "enabled,health,visible",
    [
        (False, ToolHealthStatus.HEALTHY, False),
        (True, ToolHealthStatus.UNKNOWN, False),
        (True, ToolHealthStatus.UNHEALTHY, False),
        (True, ToolHealthStatus.HEALTHY, True),
    ],
)
def test_mobsf_requires_optin_and_exact_health(enabled, health, visible):
    tool = ToolDescriptor(
        tool_id="analysis.mobsf_submit_apk",
        server_id="mobsf",
        tool_name="mobsf_submit_apk",
        capability_tags=["analysis"],
        operations=["static_analysis"],
        health_status=health,
    )
    catalog = FakeCatalog([tool])
    gate = MobileMCPEligibility(catalog, catalog, engagement(enabled))
    assert bool(gate.get(tool.tool_id)) == visible
    assert bool(gate.list_tools_for_action("analysis", "static_analysis")) == visible


def test_scoped_adapter_rejects_forged_run():
    now = datetime.now(timezone.utc)
    invocation = ToolInvocation(
        invocation_id="i",
        action_id="a",
        run_id="run",
        tool_id="analysis.mobsf_submit_apk",
        operation="static_analysis",
        arguments={"run_id": "other"},
        created_at=now,
        execution_context=ExecutionContext(engagement_id="eng", run_id="run"),
    )
    with pytest.raises(ValueError, match="context_mismatch"):
        MobSFMCPAdapter(server_id="mobsf", endpoint="http://127.0.0.1:1").bind_context(invocation)


def test_permissions_secrets_and_network_do_not_become_findings():
    from redteam.core.contracts.target import AssetTarget

    now = datetime.now(timezone.utc)
    result = ToolResult(
        invocation_id="i",
        action_id="a",
        tool_id="analysis.mobsf_report_summary",
        success=True,
        started_at=now,
        finished_at=now,
        output={
            "success": True,
            "status": "completed",
            "sections": {
                "permission": {"names": ["android.permission.SEND_SMS"]},
                "secret": {"count": 1},
                "network": {"count": 1},
            },
        },
    )
    assert (
        map_mobsf_report(target=AssetTarget(asset_id="apk"), arguments={}, result=result).findings
        == []
    )


async def test_disabled_gate_never_forwards_an_invocation():
    tool = ToolDescriptor(
        tool_id="analysis.mobsf_submit_apk",
        server_id="mobsf",
        tool_name="mobsf_submit_apk",
        capability_tags=["analysis"],
        operations=["static_analysis"],
        health_status=ToolHealthStatus.HEALTHY,
    )

    class Gateway:
        calls = 0

        async def invoke(self, invocation):
            self.calls += 1
            raise AssertionError("disabled call reached the underlying Gateway")

    gateway = Gateway()
    gate = MobileMCPEligibility(FakeCatalog([tool]), gateway, engagement(False))
    now = datetime.now(timezone.utc)
    result = await gate.invoke(
        ToolInvocation(
            invocation_id="i",
            action_id="a",
            run_id="run",
            tool_id=tool.tool_id,
            operation="static_analysis",
            arguments={"apk_ref": "apk", "expected_sha256": "a" * 64},
            execution_context=ExecutionContext(engagement_id="eng", run_id="run"),
            created_at=now,
        )
    )
    assert not result.success and result.error.code == "mobile_mcp_ineligible"
    assert gateway.calls == 0


async def test_primary_recovery_blocks_a_previously_selected_fallback():
    primary = ToolDescriptor(
        tool_id="analysis.decode_apk",
        server_id="apktool",
        tool_name="decode_apk",
        capability_tags=["analysis"],
        operations=["static_analysis"],
        health_status=ToolHealthStatus.UNHEALTHY,
    )
    fallback = primary.model_copy(
        update={
            "tool_id": "analysis.apk_file_listing",
            "tool_name": "apk_file_listing",
            "server_id": "analysis-local",
            "health_status": ToolHealthStatus.HEALTHY,
        }
    )
    catalog = FakeCatalog([primary, fallback])

    class Gateway:
        calls = 0

        async def refresh_health(self, **kwargs):
            primary.health_status = ToolHealthStatus.HEALTHY

        async def invoke(self, invocation):
            self.calls += 1
            raise AssertionError("stale fallback reached invoke")

    gateway = Gateway()
    gate = MobileMCPEligibility(catalog, gateway, None)
    assert gate.get(fallback.tool_id) is not None
    result = await gate.invoke(
        ToolInvocation(
            invocation_id="i",
            action_id="a",
            run_id="run",
            tool_id=fallback.tool_id,
            operation="static_analysis",
            arguments={"apk_ref": "apk"},
            execution_context=ExecutionContext(engagement_id="eng", run_id="run"),
            created_at=datetime.now(timezone.utc),
        )
    )
    assert not result.success and gateway.calls == 0


async def test_auth_failure_is_unhealthy_and_cannot_leak_body(policy):
    client = MobSFClient(
        policy,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"error": "secret-api-key"})
        ),
    )
    assert not await client.healthy()
    with pytest.raises(MobSFError, match="upload_failed") as error:
        await client.submit("apk", policy.hashes["apk"], "eng", "run")
    assert "secret-api-key" not in str(error.value)
