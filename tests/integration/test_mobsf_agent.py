"""Agent -> real Control -> Gateway/MCP -> Evidence, with optional real Docker REST."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
)
from tests.unit.test_mobsf import client_for  # noqa: F401
from tests.unit.test_mobsf import policy as mobsf_policy  # noqa: F401

SUBMIT_TOOL = "analysis.mobsf.mobsf_submit_apk"
START_TOOL = "analysis.mobsf.mobsf_start_scan"
STATUS_TOOL = "analysis.mobsf.mobsf_scan_status"
REPORT_TOOL = "analysis.mobsf.mobsf_report_summary"
REPORT_SECTIONS = ["manifest", "permission", "secret", "network"]


def _remote_executed_evidence(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or not message.get("tool_call_id"):
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("ok") is not True:
            continue
        if body.get("executed") is not True:
            continue
        for identity in body.get("evidence_ids") or []:
            text = str(identity)
            if text and text not in found:
                found.append(text)
    return found


def _tool_result(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


class NativeMobSFLLM:
    """Native MobSF pipeline: four static-analysis calls, then real settlement."""

    def __init__(self, *, apk_ref: str, expected_sha256: str, run_id: Any) -> None:
        self._apk_ref = apk_ref
        self._expected_sha256 = expected_sha256
        self._run_id = run_id
        self.evidence_store: Any = None
        self.turns = 0
        self.reviews: list[str] = []
        self._task_call: str | None = None
        self._goal_call: str | None = None

    @staticmethod
    def _wanted(tools: list[Any]) -> tuple[str, str, str, str, str, str]:
        names = {str(getattr(spec, "name", "")) for spec in tools}
        submit = exposed_tool_name(SUBMIT_TOOL)
        start = exposed_tool_name(START_TOOL)
        status = exposed_tool_name(STATUS_TOOL)
        report = exposed_tool_name(REPORT_TOOL)
        task_settle = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_settle = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert submit in names, "native model needs the MobSF submit tool"
        assert start in names, "native model needs the MobSF start tool"
        assert status in names, "native model needs the MobSF status tool"
        assert report in names, "native model needs the MobSF report tool"
        assert task_settle in names, "native model needs the task settlement helper"
        assert goal_settle in names, "native model needs the goal settlement helper"
        return submit, start, status, report, task_settle, goal_settle

    async def _current_scan_ref(self) -> str | None:
        store = self.evidence_store
        if store is None:
            return None
        records = await store.list_for_run(self._run_id)
        for record in records:
            ref = (record.metadata or {}).get("tool_result", {}).get("output", {}).get("scan_ref")
            if ref:
                return str(ref)
        return None

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        submit, start, status, report, task_tool, goal_tool = self._wanted(tools)
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        scan_ref = await self._current_scan_ref()
        if self.turns == 1:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-submit",
                        name=submit,
                        arguments={
                            "apk_ref": self._apk_ref,
                            "expected_sha256": self._expected_sha256,
                        },
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            if scan_ref is None:
                return NativeCompletion(tool_calls=(), text="holding for owned scan")
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-start",
                        name=start,
                        arguments={"apk_ref": self._apk_ref, "scan_ref": scan_ref},
                    ),
                ),
                text="",
            )
        if self.turns == 3:
            if scan_ref is None:
                return NativeCompletion(tool_calls=(), text="holding for owned scan")
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-status",
                        name=status,
                        arguments={"apk_ref": self._apk_ref, "scan_ref": scan_ref},
                    ),
                ),
                text="",
            )
        if self.turns == 4:
            if scan_ref is None:
                return NativeCompletion(tool_calls=(), text="holding for owned scan")
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-report",
                        name=report,
                        arguments={
                            "apk_ref": self._apk_ref,
                            "scan_ref": scan_ref,
                            "sections": list(REPORT_SECTIONS),
                        },
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            if not evidenced:
                return NativeCompletion(tool_calls=(), text="holding for tool evidence")
            self._task_call = "call-task-settle"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "MobSF pipeline evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="holding for task settlement")
        if self._goal_call is None:
            self._goal_call = "call-goal-settle"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "MobSF pipeline evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")

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
                "meaning": "mobsf pipeline",
                "outcome": "success",
                "reason": "scripted meaning",
                "confidence": 0.8,
                "claim_supported": True,
            }
        if route == "task_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload["verified_source_ids"])
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "MobSF static-analysis result observed",
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
            assert receipts, "goal review needs the verified executed call evidence"
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
                                "description": "MobSF static-analysis result observed",
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
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""












@pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_MOBSF_DOWN") != "1",
    reason="operator must stop only the scoped MobSF container for this live check",
)
async def test_real_stopped_mobsf_has_no_executable_candidates():
    from redteam.capability.servers.mobsf import MobSFPolicy
    from redteam.core.contracts.target import AssetTarget
    from redteam.core.enums import ToolHealthStatus
    from redteam.orchestration.agents.base import PlanStep
    from redteam.orchestration.candidates.compiler import CandidateCompiler
    from redteam.orchestration.candidates.models import CandidateDraft
    from redteam.orchestration.mobile_mcp_binding import MobileMCPEligibility

    config = json.loads(Path(os.environ["REDTEAM_TEST_MOBSF_CONFIG"]).read_text(encoding="utf-8"))
    for name, value in config["environment"].items():
        os.environ[name] = value
    configured = MobSFPolicy.load(config["policy_path"], config["policy_sha256"])
    apk_ref = config["apk_ref"]
    engagement = Engagement(
        engagement_id=configured.engagement_id,
        name="MobSF stopped-container proof",
        scope=EngagementScope(allowed_assets=[apk_ref]),
        execution_budget=ExecutionBudget(max_actions=1),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata={"mobsf_agent": {"enabled": True, "assets": configured.hashes}},
    )
    async with open_mcp_tool_gateway(["mobsf"]) as gateway:
        snapshot = await gateway.refresh_health(force=True)
        assert snapshot["mobsf"] is ToolHealthStatus.UNHEALTHY
        gate = MobileMCPEligibility(gateway, gateway, engagement)
        plan = PlanStep(
            step_id="submit-approved-apk",
            capability="analysis",
            operation="static_analysis",
            tool_id="analysis.mobsf_submit_apk",
            compatible_tool_ids=["analysis.mobsf_submit_apk"],
            require_healthy_tool=True,
            target=AssetTarget(asset_id=apk_ref),
            reason="stopped-container rejection",
            arguments={"apk_ref": apk_ref, "expected_sha256": config["sha256"]},
        )
        candidates = CandidateCompiler(gate).compile_drafts(
            [
                CandidateDraft(
                    agent_id="analysis-agent", plan_step=plan, target_node_ids=[f"asset:{apk_ref}"]
                ),
            ]
        )
        assert not candidates and not gate.list_by_capability("analysis")
        result = {
            "health": "unhealthy",
            "discovered_tools": len(gateway.all()),
            "candidate_count": len(candidates),
        }
    Path(config["agent_artifact_dir"]).joinpath("stopped-health.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result))
