"""Autonomous mobile-analysis path through Control, Gateway, Evidence and Graph."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import ApprovalStatus, GraphNodeType, ToolHealthStatus
from redteam.core.ids import AgentId, EngagementId, ToolId
from redteam.orchestration.agents import (
    ANALYSIS_AGENT,
    AgentRegistry,
    builtin_agent_definitions,
)
from redteam.orchestration.events import CANDIDATE_REJECTED
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import build_test_application

MANIFEST = """<manifest xmlns:android="http://schemas.android.com/apk/res/android"
  package="com.android.insecurebankv2">
  <uses-permission android:name="android.permission.SEND_SMS" />
  <application android:debuggable="true">
    <activity android:name=".PostLogin" android:exported="true" />
  </application>
</manifest>"""


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


def _descriptor(name: str, required: list[str]) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(f"analysis.{name}"),
        server_id="apktool-test",
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
        health_status=ToolHealthStatus.HEALTHY,
    )


def _gateway(
    adapter: ScriptedApktoolAdapter,
    *, decode_required: list[str] | None = None,
) -> MCPToolGateway:
    registry = MCPToolRegistry()
    descriptors = [
        _descriptor("decode_apk", decode_required or ["apk_path"]),
        _descriptor("get_manifest", ["project_dir"]),
        _descriptor("list_smali_files", ["project_dir"]),
        _descriptor("search_in_files", ["project_dir", "search_pattern"]),
    ]
    for descriptor in descriptors:
        registry.register(descriptor)
    return MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        {str(item.tool_id): ["static_analysis"] for item in descriptors},
    )


def _engagement(*, require_approval: bool = False) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_mobile_analysis_e2e"),
        name="InsecureBankv2 analysis E2E",
        scope=EngagementScope(allowed_assets=["insecurebankv2"]),
        execution_budget=ExecutionBudget(max_actions=8),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(
            require_approval_for=["analysis.static_analysis"] if require_approval else []
        ),
        created_at=datetime.now(timezone.utc),
        metadata={
            "asset_ref": "/work/input/InsecureBankv2.apk",
            "apk_ref": "insecurebankv2",
        },
    )


def _deps(
    engagement: Engagement,
    gateway: MCPToolGateway,
    *,
    settings: OrchestrationSettings | None = None,
):
    control = DefaultControlPlane({engagement.engagement_id: engagement})
    analysis_definition = next(
        item
        for item in builtin_agent_definitions()
        if item.agent_id == ANALYSIS_AGENT.agent_id
    )
    deps = build_test_application(
        control_plane=control,
        tool_gateway=gateway,
        tool_catalog=gateway,
        agents=AgentRegistry(
            [ANALYSIS_AGENT],
            definitions=[analysis_definition],
        ),
        settings=settings or OrchestrationSettings(checkpointing=True, max_steps=30),
    )
    return control, deps


async def test_analysis_agent_autonomously_reaches_manifest_findings() -> None:
    engagement = _engagement()
    adapter = ScriptedApktoolAdapter()
    gateway = _gateway(adapter)
    _, deps = _deps(engagement, gateway)

    final = await run_engagement(engagement, deps=deps)

    assert final.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
    assert final.actions_executed == 4
    assert final.finished_agents == [AgentId("analysis-agent")]
    assert adapter.calls[0][0] == "decode_apk"
    assert {name for name, _ in adapter.calls[1:]} == {
        "get_manifest",
        "list_smali_files",
        "search_in_files",
    }
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert len(evidence) == 4
    service = AttackGraphService(deps.graph_store)
    findings = await service.list_findings(verified=True)
    assert {item.metadata["check_id"] for item in findings} == {
        "android_manifest_debuggable",
        "android_manifest_exported_components",
        "android_manifest_dangerous_permissions",
    }
    assets = await service.query(node_type=GraphNodeType.ASSET)
    assert facts_of(assets[0].properties)["project_dir"] == "/work/output/InsecureBankv2"
    assert (await service.integrity_report()).blocking == []


async def test_analysis_failure_keeps_evidence_without_a_finding() -> None:
    engagement = _engagement()
    adapter = ScriptedApktoolAdapter(fail_decode=True)
    gateway = _gateway(adapter)
    _, deps = _deps(engagement, gateway)

    final = await run_engagement(engagement, deps=deps)

    assert final.actions_executed == 1
    assert len(await deps.evidence_store.list_for_run(final.run_id)) == 1
    assert await AttackGraphService(deps.graph_store).list_findings() == []
    work = await deps.work_store.list_items(run_id=final.run_id)
    assert [item.status for item in work] == [WorkStatus.FAILED]


async def test_analysis_approval_resumes_the_same_run_without_bypassing_control(
    tmp_path,
) -> None:
    engagement = _engagement(require_approval=True)
    adapter = ScriptedApktoolAdapter()
    gateway = _gateway(adapter)
    settings = OrchestrationSettings(
        checkpointing=True,
        checkpoint_backend="sqlite",
        checkpoint_path=str(tmp_path / "mobile-analysis-approval.sqlite3"),
        max_steps=30,
    )
    control, deps = _deps(engagement, gateway, settings=settings)

    stopped = await run_engagement(engagement, deps=deps)
    assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
    assert stopped.pending_approval_id is not None
    assert adapter.calls == []

    await control.resolve_approval(
        stopped.pending_approval_id,
        ApprovalStatus.APPROVED,
        "operator",
    )
    resumed = await AgentLoopRunner(deps, settings=deps.settings).reconcile_once(
        stopped,
        timeout_seconds=0.05,
    )

    assert resumed.resumed is True
    assert resumed.runtime.run_id == stopped.run_id
    assert resumed.runtime.actions_executed == 1
    assert [name for name, _ in adapter.calls] == ["decode_apk"]
    assert resumed.runtime.pending_approval_id != stopped.pending_approval_id


async def test_analysis_gateway_timeout_is_evidence_not_a_finding(
) -> None:
    engagement = _engagement()
    adapter = ScriptedApktoolAdapter(delay=0.05)
    gateway = _gateway(adapter)
    _, deps = _deps(
        engagement,
        gateway,
        settings=OrchestrationSettings(tool_timeout_seconds=0.01, max_steps=30),
    )

    final = await run_engagement(engagement, deps=deps)

    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert final.actions_executed == 1
    assert len(evidence) == 1
    assert evidence[0].metadata["success"] is False
    assert await AttackGraphService(deps.graph_store).list_findings() == []


async def test_analysis_schema_drift_rejects_candidate_before_gateway_call() -> None:
    engagement = _engagement()
    adapter = ScriptedApktoolAdapter()
    gateway = _gateway(adapter, decode_required=["apk_path", "new_required_field"])
    _, deps = _deps(engagement, gateway)

    final = await run_engagement(engagement, deps=deps)

    assert final.actions_executed == 0
    assert adapter.calls == []
    rejected = deps.event_bus.history(event_type=CANDIDATE_REJECTED)
    assert rejected
    assert rejected[-1].payload["reason"] == "missing_required_argument"
    assert rejected[-1].payload["field"] == "new_required_field"
