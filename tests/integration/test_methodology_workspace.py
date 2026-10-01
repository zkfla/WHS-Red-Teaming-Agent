"""Actual file MCP for reference bundles; no new Skill Tool/automatic execution."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.attack_graph.identity import node_id_for_target
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.methodology import provision_bundle, verify_bundle
from redteam.capability.workspace_manifest import WORKSPACE_READ_TOOLS
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import AssetTarget
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, RunId, ToolId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from tests.unit.test_methodology import (  # noqa: F401
    fixture_source,
    restore_test_directory_permissions,
)


def configure(monkeypatch, tmp_path, policy):
    file = tmp_path / "workspace-policy.json"
    file.write_text(policy.model_dump_json())
    values = {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": sys.executable,
        "ARGS_JSON": json.dumps(
            ["-m", "redteam.capability.servers.workspace", "--policy", str(file), "--mode", "read"]
        ),
        "PROFILE": "workspace_read",
        "WORKSPACE_ASSET_REF": policy.asset_ref,
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": json.dumps(list(WORKSPACE_READ_TOOLS)),
        "TOOL_OPERATIONS_JSON": json.dumps(
            {n: [s.operation] for n, s in WORKSPACE_READ_TOOLS.items()}
        ),
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"REDTEAM_MCP_METHOD_TEST_{suffix}", value)


async def read_through_control(gateway, policy, path):
    target = AssetTarget(asset_id=AssetId(policy.asset_ref))
    draft = CandidateDraft(
        agent_id=AgentId("reference-agent"),
        target_node_ids=[node_id_for_target(target)],
        plan_step=PlanStep(
            step_id="read-reference",
            capability="analysis",
            operation="file_read",
            tool_id=ToolId("analysis.read_file"),
            target=target,
            arguments={"path": path},
            reason="read reference data only",
            assessment_kind="observation",
        ),
    )
    compiled, rejected = CandidateCompiler(gateway).compile_drafts_with_rejections([draft])
    assert len(compiled) == 1 and not rejected
    step = compiled[0].plan_step
    engagement = Engagement(
        engagement_id=EngagementId(policy.engagement_id),
        name="reference data",
        scope=EngagementScope(allowed_assets=[policy.asset_ref]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=5),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement}, risk_resolver=RegistryRiskResolver(gateway)
    )
    proposal = ActionProposal(
        action_id=ActionId("action-reference"),
        run_id=RunId(policy.run_id),
        engagement_id=engagement.engagement_id,
        agent_id=draft.agent_id,
        capability=step.capability,
        operation=step.operation,
        target=target,
        arguments=step.arguments,
        reason=step.reason,
        created_at=datetime.now(timezone.utc),
    )
    decision = await control.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.ALLOW
    result = await gateway.invoke(build_tool_invocation(proposal, decision, tool_id=step.tool_id))
    assert result.success, result
    proposal.arguments["asset_ref"] = "other"
    assert (
        await control.evaluate(proposal, ControlContext(engagement_id=engagement.engagement_id))
    ).verdict == PolicyVerdict.DENY
    return result.output


async def test_reference_data_actual_mcp_and_candidate_control(monkeypatch, tmp_path):
    source, catalog = fixture_source(tmp_path)
    policy = provision_bundle(
        {"test": source},
        tmp_path / "bundle",
        asset_ref="methods-test",
        engagement_id="eng-test",
        run_id="run-test",
        catalog=catalog,
    )
    index = verify_bundle(policy, catalog=catalog)
    configure(monkeypatch, tmp_path, policy)
    async with open_mcp_tool_gateway(["method-test"], health_refresh_enabled=False) as gateway:
        assert {str(t.tool_id) for t in gateway.all()} == {
            "analysis.list_directory",
            "analysis.read_file",
            "analysis.search_files",
        }
        assert gateway.get(ToolId("analysis.write_file")) is None
        output = await read_through_control(gateway, policy, "references/test-entry.md")
        assert output["sha256"] == index["files"]["references/test-entry.md"]
        assert "Ignore previous instructions" in output["content"]  # remains data, not executed
        metadata = await read_through_control(gateway, policy, "catalog.json")
        assert json.loads(metadata["content"])["execution_permission"] is False


async def test_pinned_real_reference_bundle_mcp(monkeypatch, tmp_path):
    names = {
        "m4vic": "REDTEAM_METHOD_M4VIC_SOURCE",
        "rifteo": "REDTEAM_METHOD_RIFTEO_SOURCE",
        "vdb": "REDTEAM_METHOD_VDB_SOURCE",
    }
    if not all(os.environ.get(name) for name in names.values()):
        pytest.skip("set three pinned methodology checkout paths; no source download in tests")
    policy = provision_bundle(
        {key: Path(os.environ[name]) for key, name in names.items()},
        tmp_path / "bundle",
        asset_ref="methods-test",
        engagement_id="eng-test",
        run_id="run-test",
    )
    index = verify_bundle(policy)
    assert len(index["files"]) == 27  # 19 references + 3 licenses + 5 local files
    configure(monkeypatch, tmp_path, policy)
    async with open_mcp_tool_gateway(["method-test"], health_refresh_enabled=False) as gateway:
        for path in (
            "catalog.json",
            "references/m4vic-api.md",
            "references/rifteo-idor.md",
            "references/vdb-guide.md",
        ):
            output = await read_through_control(gateway, policy, path)
            assert not output["truncated"]
            assert output["sha256"] == index["files"][path]
