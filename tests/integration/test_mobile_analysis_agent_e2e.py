"""Autonomous mobile-analysis path through Control, Gateway, Evidence and Graph (native)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.adapters.mcp.base import MCPAdapter, MCPCallResult
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import (
    GraphNodeType,
    ToolHealthStatus,
)
from redteam.core.ids import ToolId
from redteam.orchestration.agents import (
    ANALYSIS_AGENT,
)
from redteam.orchestration.candidates.compiler import CandidateCompiler

MANIFEST = """<manifest xmlns:android="http://schemas.android.com/apk/res/android"
  package="com.android.insecurebankv2">
  <uses-permission android:name="android.permission.SEND_SMS" />
  <application android:debuggable="true">
    <activity android:name=".PostLogin" android:exported="true" />
  </application>
</manifest>"""

ASSET_NODE_ID = "asset:insecurebankv2"
PATH_STEP_ID = "analyze-apk"
PROJECT_DIR = "/work/output/InsecureBankv2"








class ScriptedApktoolAdapter:
    server_id = "apktool-test"

    def __init__(self, *, fail_decode: bool = False, delay: float = 0.0) -> None:
        self.fail_decode = fail_decode
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def discover_tools(self) -> list[ToolDescriptor]:
        return []

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY

    async def invoke(self, tool: ToolDescriptor, arguments: dict) -> MCPCallResult:
        self.calls.append((tool.tool_name, dict(arguments)))
        if self.delay:
            await asyncio.sleep(self.delay)
        now = datetime.now(timezone.utc)
        if tool.tool_name == "decode_apk" and self.fail_decode:
            return MCPCallResult(
                success=False,
                error=ToolError(code="decode_failed", message="forced decode failure"),
                started_at=now,
                finished_at=now,
            )
        outputs = {
            "decode_apk": {
                "success": True,
                "output_dir": "/work/output/InsecureBankv2",
            },
            "get_manifest": {"success": True, "manifest": MANIFEST},
            "list_smali_files": {"success": True, "files": ["smali/Login.smali"]},
            "search_in_files": {"success": True, "matches": []},
        }
        return MCPCallResult(
            success=True,
            output=outputs[tool.tool_name],
            started_at=now,
            finished_at=now,
        )


class ScriptedJadxAdapter:
    server_id = "jadx-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def discover_tools(self) -> list[ToolDescriptor]:
        return []

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY

    async def invoke(self, tool: ToolDescriptor, arguments: dict) -> MCPCallResult:
        self.calls.append((tool.tool_name, dict(arguments)))
        now = datetime.now(timezone.utc)
        return MCPCallResult(
            success=True,
            output={"manifest": MANIFEST},
            started_at=now,
            finished_at=now,
        )


def _descriptor(
    name: str,
    required: list[str],
    *,
    server_id: str = "apktool-test",
    health: ToolHealthStatus = ToolHealthStatus.HEALTHY,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(f"analysis.{name}"),
        server_id=server_id,
        tool_name=name,
        input_schema={
            "type": "object",
            "properties": {key: {"type": "string"} for key in required},
            "required": required,
            "additionalProperties": False,
        },
        operations=["static_analysis"],
        target_types=["asset"],
        capability_tags=["analysis"],
        health_status=health,
    )


def _gateway(
    adapter: ScriptedApktoolAdapter,
    *,
    decode_required: list[str] | None = None,
    jadx_adapter: ScriptedJadxAdapter | None = None,
    jadx_health: ToolHealthStatus = ToolHealthStatus.HEALTHY,
) -> MCPToolGateway:
    registry = MCPToolRegistry()
    descriptors = [
        _descriptor("decode_apk", decode_required or ["apk_path"]),
        _descriptor("get_manifest", ["project_dir"]),
        _descriptor("list_smali_files", ["project_dir"]),
        _descriptor("search_in_files", ["project_dir", "search_pattern"]),
    ]
    adapters: dict[str, MCPAdapter] = {adapter.server_id: adapter}
    if jadx_adapter is not None:
        descriptors.append(
            _descriptor(
                "get_android_manifest",
                [],
                server_id=jadx_adapter.server_id,
                health=jadx_health,
            )
        )
        adapters[jadx_adapter.server_id] = jadx_adapter
    for descriptor in descriptors:
        registry.register(descriptor)
    return MCPToolGateway(
        registry,
        adapters,
        {str(item.tool_id): ["static_analysis"] for item in descriptors},
    )










async def test_optional_jadx_step_requires_a_healthy_registered_tool() -> None:
    async def compile_for(health: ToolHealthStatus):
        service = AttackGraphService(InMemoryAttackGraphStore())
        asset = await service.upsert_node(
            GraphNodeType.ASSET,
            key="insecurebankv2",
            attrs={
                "in_scope": True,
                "analysis_decoded": True,
                "manifest_analyzed": True,
                "smali_inventoried": True,
                "sensitive_strings_searched": True,
            },
            facts={
                "asset_ref": "/work/input/InsecureBankv2.apk",
                "project_dir": PROJECT_DIR,
            },
        )
        for step in ANALYSIS_AGENT.playbook[:4]:
            await service.record_attempt(asset.id, step.attempt_record_key, step=1)
        gateway = _gateway(
            ScriptedApktoolAdapter(),
            jadx_adapter=ScriptedJadxAdapter(),
            jadx_health=health,
        )
        return await CandidateCompiler(gateway).compile_playbook(
            ANALYSIS_AGENT,
            service.view(),
            await service.observe(step=2),
            step=2,
        )

    healthy = await compile_for(ToolHealthStatus.HEALTHY)
    unknown = await compile_for(ToolHealthStatus.UNKNOWN)
    unhealthy = await compile_for(ToolHealthStatus.UNHEALTHY)

    assert [str(item.tool.tool_id) for item in healthy] == ["analysis.get_android_manifest"]
    assert unknown == []
    assert unhealthy == []
