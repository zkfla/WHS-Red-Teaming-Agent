"""Mobile Agent runtime path through Control, Gateway, Evidence and Graph."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

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
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.agents import (
    MOBILE_AGENT,
    AgentRegistry,
    builtin_agent_definitions,
)
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.runner import TerminationReason
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import build_test_application

PACKAGE = "com.android.insecurebankv2"
DEVICE_SERIAL = "emulator-5554"
DEVICE_REF = "redteam_x86"


class ScriptedMobileNativeLLM:
    def __init__(self) -> None:
        self.turns = 0
        self.reviews: list[str] = []
        self.native_turns: list[dict[str, Any]] = []

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.reviews.append(route)
        if route == "analysis_verify":
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "scripted meaning",
                "confidence": 0.8,
                "claim_supported": True,
            }
        if route == "task_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload["verified_source_ids"])
            assert receipts, "task review needs executed evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "mobile runtime observation",
                        "kind": "observation",
                        "evidence_ids": list(receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            criteria = payload["mission"]["success_criteria"]
            receipts = list(payload["verified_source_ids"])
            assert receipts, "goal review needs executed evidence"
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": item,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "mobile runtime observation",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for item in criteria
                ],
            }
        raise AssertionError("legacy strategy route is not part of the native loop")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""

    def _exposed(self, tools: list[Any], tool_id: str) -> str:
        names = {str(getattr(spec, "name", "")) for spec in tools}
        name = exposed_tool_name(tool_id)
        assert name in names, "native model needs the registered mobile tool"
        return name

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        self.native_turns.append({"messages": messages, "tools": tools})
        self.turns += 1
        launch = self._exposed(tools, "mobile.mobile_launch_app")
        logs = self._exposed(tools, "mobile.mobile_get_device_logs")
        listed = self._exposed(tools, "mobile.mobile_list_crashes")
        detail = self._exposed(tools, "mobile.mobile_get_crash")
        if self.turns == 1:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-launch-1",
                        name=launch,
                        arguments={"device": DEVICE_REF, "packageName": PACKAGE},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-logs-2",
                        name=logs,
                        arguments={
                            "device": DEVICE_REF,
                            "filter": [f"process={PACKAGE}"],
                            "limit": 200,
                        },
                    ),
                ),
                text="",
            )
        if self.turns == 3:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-list-3",
                        name=listed,
                        arguments={"device": DEVICE_REF},
                    ),
                ),
                text="",
            )
        if self.turns == 4:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-crash-4",
                        name=detail,
                        arguments={"device": DEVICE_REF, "id": "bank-crash-1"},
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="holding for review")


def _runtime_llm() -> ScriptedMobileNativeLLM:
    return ScriptedMobileNativeLLM()


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
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        outbound_descriptor_resolver=gateway.get,
    )
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
