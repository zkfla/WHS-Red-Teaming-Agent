"""Mobile Agent runtime path through Control, Gateway, Evidence and Graph."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.predicates import attrs_of, facts_of
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
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import EvidenceSourceType, GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.agents import (
    MOBILE_AGENT,
    AgentRegistry,
    builtin_agent_definitions,
)
from redteam.orchestration.runner import TerminationReason
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import build_test_application
from tests.integration._engine_llm_harness import ScriptedLLM, review_accept

PACKAGE = "com.android.insecurebankv2"
DEVICE_SERIAL = "emulator-5554"
DEVICE_REF = "redteam_x86"
ASSET_NODE_ID = "asset:insecurebankv2"
PATH_STEP_ID = "observe-mobile-runtime"


def _draft_path() -> dict:
    return {
        "path_id": "path-mobile-runtime",
        "run_id": "run-scripted",
        "mission_id": None,
        "version": 1,
        "parent_version": None,
        "status": "draft",
        "reason": "scripted mobile runtime path",
        "goal": "launch the authorized app and observe logs and crashes",
        "steps": [
            {
                "step_id": PATH_STEP_ID,
                "objective": "launch the app and collect bounded runtime observations",
                "agent_id": "mobile-agent",
                "target_node_ids": [ASSET_NODE_ID],
                "principal_refs": [],
                "allowed_operations": ["mobile.app_launch", "mobile.response_analysis"],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {
                        "kind": "evidence",
                        "description": "mobile runtime evidence recorded",
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


def _turn_action(tool_id: str, operation: str, arguments: dict | None = None) -> dict:
    return {
        "mode": "actions",
        "reason": f"scripted {tool_id} action",
        "actions": [
            {
                "path_step_id": PATH_STEP_ID,
                "agent_id": "mobile-agent",
                "target_node_id": ASSET_NODE_ID,
                "principal_ref": None,
                "capability": "mobile",
                "operation": operation,
                "tool_id": tool_id,
                "arguments": arguments or {},
                "reason": f"run {tool_id} against the configured device",
                "expected_observation": "bounded app runtime observation",
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
        "reason": "scripted hold after runtime observations",
        "actions": [],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def _runtime_llm() -> ScriptedLLM:
    llm = ScriptedLLM()
    llm.add("path_draft", _draft_path())
    llm.add("path_review", review_accept())
    llm.add("next_action", _turn_action("mobile.mobile_launch_app", "app_launch"))
    llm.add(
        "next_action",
        _turn_action(
            "mobile.mobile_get_device_logs",
            "response_analysis",
            {"filter": [f"process={PACKAGE}"], "limit": 200},
        ),
    )
    llm.add("next_action", _turn_action("mobile.mobile_list_crashes", "response_analysis"))
    llm.add(
        "next_action",
        _turn_action(
            "mobile.mobile_get_crash",
            "response_analysis",
            {"id": "bank-crash-1"},
        ),
    )
    llm.add("next_action", _turn_hold())
    return llm


class ScriptedMobileNextAdapter:
    server_id = "mobile-next-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def discover_tools(self) -> list[ToolDescriptor]:
        return []

    async def health_check(self) -> ToolHealthStatus:
        return ToolHealthStatus.HEALTHY

    async def invoke(self, tool: ToolDescriptor, arguments: dict) -> MCPCallResult:
        self.calls.append((tool.tool_name, dict(arguments)))
        now = datetime.now(timezone.utc)
        outputs = {
            "mobile_launch_app": f"Launched app {PACKAGE}",
            "mobile_get_device_logs": json.dumps(
                {
                    "process": PACKAGE,
                    "tag": "AndroidRuntime",
                    "level": "E",
                    "message": "FATAL EXCEPTION: main",
                }
            ),
            "mobile_list_crashes": json.dumps(
                [
                    {
                        "id": "bank-crash-1",
                        "packageName": PACKAGE,
                        "timestamp": now.isoformat(),
                    }
                ]
            ),
            "mobile_get_crash": (
                f"Process: {PACKAGE}\nFATAL EXCEPTION: main\njava.lang.IllegalStateException"
            ),
        }
        return MCPCallResult(
            success=True,
            output=outputs[tool.tool_name],
            started_at=now,
            finished_at=now,
        )


def _descriptor(
    name: str,
    operation: str,
    required: list[str],
    optional: dict[str, dict] | None = None,
) -> ToolDescriptor:
    properties = {key: {"type": "string"} for key in required}
    properties.update(optional or {})
    return ToolDescriptor(
        tool_id=ToolId(f"mobile.{name}"),
        server_id="mobile-next-test",
        tool_name=name,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        operations=[operation],
        target_types=["device"],
        capability_tags=["mobile"],
        risk_level=RiskLevel.MEDIUM,
        health_status=ToolHealthStatus.HEALTHY,
    )


def _gateway(adapter: ScriptedMobileNextAdapter) -> MCPToolGateway:
    descriptors = [
        _descriptor("mobile_launch_app", "app_launch", ["device", "packageName"]),
        _descriptor(
            "mobile_get_device_logs",
            "response_analysis",
            ["device"],
            {
                "filter": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
        ),
        _descriptor("mobile_list_crashes", "response_analysis", ["device"]),
        _descriptor("mobile_get_crash", "response_analysis", ["device", "id"]),
    ]
    registry = MCPToolRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    return MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        {str(item.tool_id): [item.operations[0]] for item in descriptors},
    )


def _engagement() -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_mobile_runtime_e2e"),
        name="InsecureBankv2 runtime E2E",
        scope=EngagementScope(allowed_assets=["insecurebankv2"]),
        execution_budget=ExecutionBudget(max_actions=6),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata={
            "device_serial": DEVICE_SERIAL,
            "device_ref": DEVICE_REF,
            "package_name": PACKAGE,
        },
    )


async def test_mobile_agent_reaches_runtime_finding_and_crash_followup() -> None:
    engagement = _engagement()
    adapter = ScriptedMobileNextAdapter()
    gateway = _gateway(adapter)
    control = DefaultControlPlane({engagement.engagement_id: engagement})
    mobile_definition = next(
        item
        for item in builtin_agent_definitions()
        if item.agent_id == MOBILE_AGENT.agent_id
    )
    deps = build_test_application(
        control_plane=control,
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=_runtime_llm(),
        agents=AgentRegistry([MOBILE_AGENT], definitions=[mobile_definition]),
        settings=OrchestrationSettings(checkpointing=True, max_steps=40),
    )

    final = await run_engagement(engagement, deps=deps)

    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
    assert final.actions_executed == 4
    call_names = [name for name, _ in adapter.calls]
    assert call_names[0] == "mobile_launch_app"
    assert set(call_names[1:]) == {
        "mobile_get_device_logs",
        "mobile_list_crashes",
        "mobile_get_crash",
    }
    assert call_names.index("mobile_list_crashes") < call_names.index("mobile_get_crash")
    calls = dict(adapter.calls)
    assert adapter.calls[0][1] == {"device": DEVICE_REF, "packageName": PACKAGE}
    assert calls["mobile_get_device_logs"] == {
        "device": DEVICE_REF,
        "filter": [f"process={PACKAGE}"],
        "limit": 200,
    }
    assert calls["mobile_get_crash"] == {"device": DEVICE_REF, "id": "bank-crash-1"}

    evidence = await deps.evidence_store.list_for_run(final.run_id)
    tool_evidence = [item for item in evidence if item.source_type is EvidenceSourceType.TOOL]
    assert len(tool_evidence) == 4
    assert all(item.metadata["success"] is True for item in tool_evidence)

    graph = AttackGraphService(deps.graph_store)
    [asset] = await graph.query(node_type=GraphNodeType.ASSET)
    expected_attrs = {
        "mobile_crash_observed": True,
        "mobile_runtime_crash_observed": True,
    }
    assert attrs_of(asset.properties).items() >= expected_attrs.items()
    facts = facts_of(asset.properties)
    assert facts["crash_id"] == "bank-crash-1"
    assert facts["mobile_log_assessment"]["verdict"] == "violation"
    assert facts["mobile_crash_detail_assessment"]["verdict"] == "violation"

    findings = await graph.list_findings(verified=True)
    assert [item.metadata["check_id"] for item in findings] == ["android_runtime_crash"]
    assert len(findings[0].evidence_ids) == 2
    assert (await graph.integrity_report()).blocking == []
