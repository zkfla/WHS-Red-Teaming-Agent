"""Configured MCP capacity reaches the application gateway for owned providers."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability import bootstrap as capability_bootstrap
from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
from redteam.capability.adapters.mcp.transport import RemoteCallResult, RemoteTool
from redteam.capability.servers.analysis import AnalysisServerPolicy
from redteam.capability.servers.config import ServerPolicy, TargetProfile
from redteam.capability.servers.mobile import DeviceProfile, MobileDiagnosticsPolicy
from redteam.capability.servers.mobsf import MobSFPolicy
from redteam.capability.servers.vdb_readonly import VDBPolicy
from redteam.capability.settings import MCPServerSettings, environment_prefix
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId
from redteam.core.tool_binding import SELECTED_TOOL

_PROVIDERS = (
    ("recon-active", "recon", "server"),
    ("mobile-diagnostics", "mobile", "mobile"),
    ("web-proxy", "web_api", "server"),
    ("web-zap", "web_api", "server"),
    ("web-nuclei", "web_api", "server"),
    ("mobsf", "analysis", "mobsf"),
    ("vdb-readonly", "analysis", "vdb"),
)
_MOBSF_IMAGE = "opensecurity/mobile-security-framework-mobsf:v4.4.6@sha256:" + "a" * 64


class _GatewayCallBarrier:
    """Fake MCP transport that records real gateway admission overlap."""

    def __init__(self, expected_capacity: int) -> None:
        self.expected_capacity = expected_capacity
        self.tools = [RemoteTool("capacity_probe", "", {"type": "object"})]
        self.active = 0
        self.peak = 0
        self.entered_capacity = asyncio.Event()
        self.overflow = asyncio.Event()
        self.release = asyncio.Event()

    async def list_tools(self) -> list[RemoteTool]:
        return self.tools

    async def health_check(self) -> bool:
        return True

    async def call_tool(self, tool_name: str, arguments: dict) -> RemoteCallResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= self.expected_capacity:
            self.entered_capacity.set()
        if self.active > self.expected_capacity:
            self.overflow.set()
        try:
            await self.release.wait()
            return RemoteCallResult(is_error=False, output={"tool": tool_name})
        finally:
            self.active -= 1


def _provider_policy(kind: str, capacity: int, tmp_path: Path):
    if kind == "server":
        return ServerPolicy(
            targets={
                "lab": TargetProfile(origin="http://lab.local", allowed_paths=["/"]),
            },
            allowed_networks=["203.0.113.0/24"],
            max_concurrency=capacity,
        ), "max_concurrency"
    if kind == "mobile":
        return MobileDiagnosticsPolicy(
            devices={"lab-device": DeviceProfile(serial="emulator-5554")},
            max_concurrency=capacity,
        ), "max_concurrency"
    if kind == "mobsf":
        apk = tmp_path / "approved.apk"
        apk.write_bytes(b"fake-apk-policy-fixture")
        api_key = tmp_path / "mobsf-api-key"
        api_key.write_text("fixture-only", encoding="utf-8")
        return MobSFPolicy(
            enabled=True,
            endpoint="http://127.0.0.1:18000",
            api_key_file=str(api_key),
            image=_MOBSF_IMAGE,
            workspace_root=str(tmp_path),
            analysis=AnalysisServerPolicy.model_validate({
                "apks": {"approved": {"path": str(apk)}},
            }),
            hashes={"approved": hashlib.sha256(apk.read_bytes()).hexdigest()},
            engagement_id="eng-capacity-test",
            run_id="run-capacity-test",
            state_path=str(tmp_path / "mobsf-state.sqlite3"),
            max_concurrency=capacity,
        ), "max_concurrency"
    if kind == "vdb":
        return VDBPolicy(
            asset_ref="vdb-capacity-test",
            engagement_id="eng-capacity-test",
            run_id="run-capacity-test",
            root=str(tmp_path),
            synthetic=True,
            max_concurrent_lookups=capacity,
        ), "max_concurrent_lookups"
    raise AssertionError(f"unknown provider policy kind: {kind}")


def _capacity_environment(server_id: str, kind: str, capacity: int) -> dict[str, str]:
    prefix = environment_prefix(server_id)
    environment = {
        f"{prefix}_KIND": kind,
        f"{prefix}_ENDPOINT": "http://127.0.0.1:8765/mcp",
    }
    if capacity != 1:
        environment[f"{prefix}_MAX_CONCURRENCY"] = str(capacity)
    return environment


def _invocation(tool_id, index: int) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId(f"capacity-inv-{index}"),
        action_id=ActionId(f"capacity-action-{index}"),
        run_id=RunId("capacity-run"),
        tool_id=tool_id,
        binding_mode=SELECTED_TOOL,
        arguments={},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("capacity-engagement"),
            run_id=RunId("capacity-run"),
        ),
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(("server_id", "kind", "policy_kind"), _PROVIDERS)
@pytest.mark.parametrize("capacity", [6, 1])
async def test_owned_provider_environment_capacity_reaches_bootstrap_gateway(
    server_id: str,
    kind: str,
    policy_kind: str,
    capacity: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _capacity_environment(server_id, kind, capacity)
    settings = MCPServerSettings.from_environment(server_id, env=environment)
    assert settings.max_concurrency == capacity

    policy, policy_field = _provider_policy(policy_kind, capacity, tmp_path)
    assert getattr(policy, policy_field) == capacity
    serialized_policy = policy.model_dump(mode="json")
    if capacity == 1:
        assert policy_field not in serialized_policy
    else:
        assert serialized_policy[policy_field] == capacity

    transport = _GatewayCallBarrier(capacity)

    def fake_transport(_settings):
        return transport

    def fake_adapter(settings, provider_transport, _bundles=None):
        return MCPServerAdapter(
            server_id=settings.server_id,
            endpoint=settings.endpoint,
            tool_namespace=kind,
            capability_tags=(kind,),
            transport=provider_transport,
        )

    monkeypatch.setattr(capability_bootstrap, "_create_transport", fake_transport)
    monkeypatch.setattr(capability_bootstrap, "_create_adapter", fake_adapter)
    gateway = await capability_bootstrap.build_mcp_tool_gateway(
        [server_id], env=environment, health_refresh_enabled=False,
    )
    descriptor, = gateway.all_descriptors()
    call_count = capacity + 1
    calls = [
        asyncio.create_task(gateway.invoke(_invocation(descriptor.tool_id, index)))
        for index in range(call_count)
    ]
    try:
        await asyncio.wait_for(transport.entered_capacity.wait(), 1)
        # Let all excess invocations reach the gateway's per-server queue.
        await asyncio.sleep(0.02)
        assert transport.active == capacity
        assert transport.peak == capacity
        assert not transport.overflow.is_set()
    finally:
        transport.release.set()
    results = await asyncio.gather(*calls)

    assert all(result.success for result in results)
    assert transport.peak == capacity
