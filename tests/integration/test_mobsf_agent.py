"""Agent -> real Control -> Gateway/MCP -> Evidence, with optional real Docker REST."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.adapters.mcp.mobsf import MobSFMCPAdapter
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.servers.mobsf import create_server
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.enums import EvidenceSourceType, RiskLevel
from redteam.core.ids import RunId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.mobile_mcp_binding import wire_mobile_mcp
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import build_test_application
from tests.integration._engine_llm_harness import ScriptedLLM, review_accept
from tests.integration.test_execution_agent_e2e import InProcessTransport
from tests.integration.test_mobile_analysis_agent_e2e import _draft_path, _turn_action, _turn_hold
from tests.unit.test_mobsf import client_for  # noqa: F401
from tests.unit.test_mobsf import policy as mobsf_policy  # noqa: F401


async def exercise_agent(gateway, configured, artifact_dir: Path, *, deny=False):
    """Script the model's choices while every tool, Control decision and Evidence write is real."""
    apk_ref = next(iter(configured.hashes))
    engagement = Engagement(
        engagement_id=configured.engagement_id,
        name="Approved MobSF static-analysis lab",
        scope=EngagementScope(allowed_assets=[apk_ref]),
        execution_budget=ExecutionBudget(max_actions=8, max_duration_seconds=1200),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        # Explicit bounded lab preapproval; the real Control still checks scope/risk/ROE.
        approval_policy=ApprovalPolicy(
            auto_approve_up_to=None if deny else RiskLevel.HIGH,
            require_approval_for=["analysis.static_analysis"] if deny else [],
        ),
        rules_of_engagement=[
            RuleOfEngagement(
                name="approved-apk-static-only",
                description="Only the registered lab APK",
                allowed_capabilities=["analysis"],
                allowed_operations=["analysis.static_analysis"],
            )
        ],
        metadata={
            "apk_ref": apk_ref,
            "mobsf_agent": {
                "enabled": True,
                "assets": configured.hashes,
            },
        },
        created_at=datetime.now(timezone.utc),
    )
    run_id = RunId(configured.run_id)

    class ObservedScanLLM(ScriptedLLM):
        evidence_store = None

        async def complete_structured(self, **kwargs):
            result = await super().complete_structured(**kwargs)
            if self._route_key(kwargs["response_schema"]) == "next_action":
                for action in result.get("actions", []):
                    if action["arguments"].get("scan_ref") == "$owned_scan":
                        records = await self.evidence_store.list_for_run(run_id)
                        scans = [
                            r.metadata.get("tool_result", {}).get("output", {}).get("scan_ref")
                            for r in records
                        ]
                        action["arguments"]["scan_ref"] = next(ref for ref in scans if ref)
            return result

    llm = ObservedScanLLM()
    path = _draft_path()
    path["steps"][0]["target_node_ids"] = [f"asset:{apk_ref}"]
    llm.add("path_draft", path)
    llm.add("path_review", review_accept())
    actions = [
        (
            "analysis.mobsf_submit_apk",
            {"apk_ref": apk_ref, "expected_sha256": configured.hashes[apk_ref]},
        ),
        ("analysis.mobsf_start_scan", {"apk_ref": apk_ref, "scan_ref": "$owned_scan"}),
        ("analysis.mobsf_scan_status", {"apk_ref": apk_ref, "scan_ref": "$owned_scan"}),
        (
            "analysis.mobsf_report_summary",
            {
                "apk_ref": apk_ref,
                "scan_ref": "$owned_scan",
                "sections": ["manifest", "permission", "secret", "network"],
            },
        ),
    ]
    for tool_id, arguments in actions:
        action = _turn_action(tool_id, arguments)
        action["actions"][0]["target_node_id"] = f"asset:{apk_ref}"
        llm.add("next_action", action)
    llm.add("next_action", _turn_hold())
    deps = build_test_application(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement},
            risk_resolver=RegistryRiskResolver(gateway),
        ),
        tool_gateway=gateway,
        tool_catalog=gateway,
        evidence_store=FilesystemEvidenceStore(artifact_dir / "evidence"),
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=18, checkpointing=False, tool_timeout_seconds=600),
    )
    deps = wire_mobile_mcp(replace(deps, engagement=engagement))
    llm.evidence_store = deps.evidence_store
    started = time.monotonic()
    final = await run_engagement(engagement, deps=deps, run_id=run_id)
    evidence = await deps.evidence_store.list_for_run(run_id)
    tool_evidence = [item for item in evidence if item.source_type is EvidenceSourceType.TOOL]
    graph = AttackGraphService(deps.graph_store)
    findings = await graph.list_findings(verified=True)
    assert final.actions_executed == (0 if deny else 4)
    assert len(tool_evidence) == (0 if deny else 4)
    assert all(item.metadata["tool_result"]["success"] for item in tool_evidence)
    events = deps.event_bus.history()
    decisions = [
        event.model_dump(mode="json")
        for event in events
        if "policy" in str(event.event_type).lower()
    ]
    assert decisions, "real Control must emit a policy decision"
    outcome = {
        "seconds": round(time.monotonic() - started, 2),
        "actions": final.actions_executed,
        "tool_evidence": len(tool_evidence),
        "findings": len(findings),
        "termination": str(final.termination_reason),
        "policy_events": len(decisions),
        "llm": "scripted choices; real tools and Control",
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "agent-result.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    (artifact_dir / "tool-evidence.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in tool_evidence], indent=2),
        encoding="utf-8",
    )
    return outcome


async def mock_gateway(configured, client):
    adapter = MobSFMCPAdapter(
        server_id="mobsf",
        transport=InProcessTransport(create_server(configured)),
        risk_level=RiskLevel.HIGH,
    )
    adapter.client = client
    # Reuse the real MCP handlers, replacing only the upstream REST client in this unit path.
    import redteam.capability.servers.mobsf as module

    original = module.MobSFClient
    try:
        module.MobSFClient = lambda _: client
        adapter._transport = InProcessTransport(create_server(configured))
    finally:
        module.MobSFClient = original
    descriptors = await adapter.discover_tools()
    for tool in descriptors:
        tool.operations = ["static_analysis"]
        tool.target_types = ["asset"]
    registry = MCPToolRegistry()
    registry.replace_server_tools("mobsf", descriptors)
    return MCPToolGateway(
        registry, {"mobsf": adapter}, {str(t.tool_id): ("static_analysis",) for t in descriptors}
    )


async def test_mobsf_agent_uses_real_control_and_observed_scan_ref(mobsf_policy, tmp_path):  # noqa: F811
    client, calls = client_for(mobsf_policy)
    outcome = await exercise_agent(
        await mock_gateway(mobsf_policy, client),
        mobsf_policy,
        tmp_path / "proof",
    )
    assert outcome["findings"] == 1
    assert any(path.endswith("scan") for _, path in calls)


async def test_control_approval_pending_sends_no_upload_or_scan(mobsf_policy, tmp_path):  # noqa: F811
    client, calls = client_for(mobsf_policy)
    await exercise_agent(
        await mock_gateway(mobsf_policy, client),
        mobsf_policy,
        tmp_path / "proof",
        deny=True,
    )
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.skipif(
    not os.environ.get("REDTEAM_TEST_MOBSF_CONFIG"),
    reason="set REDTEAM_TEST_MOBSF_CONFIG to an approved live smoke configuration",
)
async def test_real_mobsf_docker_agent_e2e():
    from redteam.capability.servers.mobsf import MobSFPolicy

    config = json.loads(Path(os.environ["REDTEAM_TEST_MOBSF_CONFIG"]).read_text(encoding="utf-8"))
    for name, value in config["environment"].items():
        os.environ[name] = value
    configured = MobSFPolicy.load(config["policy_path"], config["policy_sha256"])
    async with open_mcp_tool_gateway(["mobsf"]) as gateway:
        result = await exercise_agent(gateway, configured, Path(config["agent_artifact_dir"]))
    print(json.dumps(result))


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
