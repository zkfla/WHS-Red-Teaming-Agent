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
from redteam.core.enums import (
    ApprovalStatus,
    EvidenceSourceType,
    GraphNodeType,
    ToolHealthStatus,
)
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.agents import (
    ANALYSIS_AGENT,
    AgentRegistry,
    builtin_agent_definitions,
)
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import build_test_application
from tests.integration._engine_llm_harness import ScriptedLLM, review_accept

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


def _draft_path() -> dict:
    return {
        "path_id": "path-mobile-analysis",
        "run_id": "run-scripted",
        "mission_id": None,
        "version": 1,
        "parent_version": None,
        "status": "draft",
        "reason": "scripted mobile analysis path",
        "goal": "analyze the authorized APK",
        "steps": [
            {
                "step_id": PATH_STEP_ID,
                "objective": "decode and inspect the authorized APK",
                "agent_id": "analysis-agent",
                "target_node_ids": [ASSET_NODE_ID],
                "principal_refs": [],
                "allowed_operations": ["analysis.static_analysis"],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {
                        "kind": "evidence",
                        "description": "APK analysis evidence recorded",
                        "min_evidence": 1,
                    }
                ],
                "required": True,
                "status": "pending",
                "outcome": None,
                "evidence_ids": [],
            }
        ],
        "hypothesis_ids": [],
        "evidence_ids": [],
        "changed_step_ids": [],
        "review_records": {},
    }


def _turn_action(tool_id: str, arguments: dict | None = None) -> dict:
    return {
        "mode": "actions",
        "reason": f"scripted {tool_id} action",
        "actions": [
            {
                "path_step_id": PATH_STEP_ID,
                "agent_id": "analysis-agent",
                "target_node_id": ASSET_NODE_ID,
                "principal_ref": None,
                "capability": "analysis",
                "operation": "static_analysis",
                "tool_id": tool_id,
                "arguments": arguments or {},
                "reason": f"run {tool_id} on the authorized APK",
                "expected_observation": "bounded static analysis output",
                "hypothesis": None,
                "existing_hypothesis_id": None,
                "expectation": None,
                "authentication_check": None,
                "availability_impact": False,
            }
        ],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def _turn_hold() -> dict:
    return {
        "mode": "hold",
        "reason": "scripted hold after the requested analysis actions",
        "actions": [],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def _llm_for(*actions: tuple[str, dict]) -> ScriptedLLM:
    llm = ScriptedLLM()
    llm.add("path_draft", _draft_path())
    llm.add("path_review", review_accept())
    for tool_id, arguments in actions:
        llm.add("next_action", _turn_action(tool_id, arguments))
    llm.add("next_action", _turn_hold())
    return llm


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
    llm: ScriptedLLM,
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
        llm_client=llm,
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
    llm = _llm_for(
        ("analysis.decode_apk", {"apk_path": "/work/input/InsecureBankv2.apk"}),
        ("analysis.get_manifest", {"project_dir": PROJECT_DIR}),
        ("analysis.list_smali_files", {"project_dir": PROJECT_DIR}),
        (
            "analysis.search_in_files",
            {"project_dir": PROJECT_DIR, "search_pattern": "password"},
        ),
    )
    _, deps = _deps(engagement, gateway, llm=llm)

    final = await run_engagement(engagement, deps=deps)

    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
    assert final.actions_executed == 4
    assert adapter.calls[0][0] == "decode_apk"
    assert {name for name, _ in adapter.calls[1:]} == {
        "get_manifest",
        "list_smali_files",
        "search_in_files",
    }
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    tool_evidence = [item for item in evidence if item.source_type is EvidenceSourceType.TOOL]
    assert len(tool_evidence) == 4
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
    llm = _llm_for(
        ("analysis.decode_apk", {"apk_path": "/work/input/InsecureBankv2.apk"}),
    )
    _, deps = _deps(engagement, gateway, llm=llm)

    final = await run_engagement(engagement, deps=deps)

    assert final.actions_executed == 1
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert sum(item.source_type is EvidenceSourceType.TOOL for item in evidence) == 1
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
    control, deps = _deps(
        engagement,
        gateway,
        llm=_llm_for(
            ("analysis.decode_apk", {"apk_path": "/work/input/InsecureBankv2.apk"}),
        ),
        settings=settings,
    )

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
    assert resumed.runtime.pending_approval_id is None


async def test_analysis_gateway_timeout_is_evidence_not_a_finding() -> None:
    engagement = _engagement()
    adapter = ScriptedApktoolAdapter(delay=0.05)
    gateway = _gateway(adapter)
    _, deps = _deps(
        engagement,
        gateway,
        llm=_llm_for(
            ("analysis.decode_apk", {"apk_path": "/work/input/InsecureBankv2.apk"}),
        ),
        settings=OrchestrationSettings(tool_timeout_seconds=0.01, max_steps=30),
    )

    final = await run_engagement(engagement, deps=deps)

    evidence = await deps.evidence_store.list_for_run(final.run_id)
    tool_evidence = [item for item in evidence if item.source_type is EvidenceSourceType.TOOL]
    assert final.actions_executed == 1
    assert len(tool_evidence) == 1
    assert tool_evidence[0].metadata["success"] is False
    assert await AttackGraphService(deps.graph_store).list_findings() == []


async def test_analysis_schema_drift_rejects_candidate_before_gateway_call() -> None:
    engagement = _engagement()
    adapter = ScriptedApktoolAdapter()
    gateway = _gateway(adapter, decode_required=["apk_path", "new_required_field"])
    invalid_action = (
        "analysis.decode_apk",
        {"apk_path": "/work/input/InsecureBankv2.apk"},
    )
    llm = _llm_for(invalid_action, invalid_action, invalid_action)
    _, deps = _deps(engagement, gateway, llm=llm)

    final = await run_engagement(engagement, deps=deps)

    assert final.actions_executed == 0
    assert final.termination_reason == TerminationReason.PLANNING_ERROR
    assert adapter.calls == []
    assert [call["route"] for call in llm.calls].count("next_action") == 3
