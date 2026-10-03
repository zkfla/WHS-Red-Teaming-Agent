"""A read/write + VDB profiles in ONE actual Gateway; separate assets and risks."""

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import node_id_for_target
from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.manifest import validate_tool_manifest
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
from redteam.core.convert import NonAllowDecisionError, build_tool_invocation
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, RunId, ToolId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from tests.integration.test_vdb_extended_mcp import GHSA, extended
from tests.integration.test_vdb_readonly_mcp import seeded as seeded  # noqa: F401
from tests.integration.test_workspace_mcp import configure_workspace


async def test_all_twelve_a_tools_compose_without_scope_or_risk_leakage(
    seeded,
    tmp_path,
    monkeypatch,
):
    extended(monkeypatch, tmp_path, initialized=True)
    servers = configure_workspace(monkeypatch, tmp_path) + ["vdb-test"]
    async with open_mcp_tool_gateway(servers, health_refresh_enabled=False) as gateway:
        assert len(gateway.all()) == 12
        assert len({d.tool_id for d in gateway.all()}) == 12
        for descriptor in gateway.all():
            validate_tool_manifest(descriptor)
            assert descriptor.output_schema
            assert descriptor.target_types == ["asset"]
            assert descriptor.required_arguments
        assert {
            str(d.tool_id) for d in gateway.list_tools_for_action("analysis", "file_read")
        } == {build_canonical_mcp_tool_id("analysis", "workspace-read-test", "read_file")}
        assert all(
            d.tool_name.startswith("search_")
            for d in gateway.list_tools_for_action("analysis", "static_analysis")
        )
        engagement = Engagement(
            engagement_id=EngagementId("eng-test"),
            name="synthetic A profile composition",
            scope=EngagementScope(allowed_assets=["workspace-test", "vdb-test"]),
            execution_budget=ExecutionBudget(max_actions=10),
            rate_limit_policy=RateLimitPolicy(max_actions_per_minute=100),
            approval_policy=ApprovalPolicy(),
            created_at=datetime.now(timezone.utc),
        )
        control = DefaultControlPlane(
            {engagement.engagement_id: engagement}, risk_resolver=RegistryRiskResolver(gateway)
        )
        compiler = CandidateCompiler(gateway)
        for name, operation, asset, args in (
            ("read_file", "file_read", "workspace-test", {"path": "source.txt"}),
            ("search_by_ghsa", "static_analysis", "vdb-test", {"ghsa_id": GHSA}),
            (
                "write_file",
                "file_write",
                "workspace-test",
                {"path": "must-not-exist.txt", "content": "requires human decision"},
            ),
        ):
            target = AssetTarget(asset_id=AssetId(asset))
            draft = CandidateDraft(
                agent_id=AgentId("analysis-agent"),
                target_node_ids=[node_id_for_target(target)],
                plan_step=PlanStep(
                    step_id=name,
                    capability="analysis",
                    operation=operation,
                    tool_id=ToolId(f"analysis.{name}"),
                    target=target,
                    arguments=args,
                    reason="approved synthetic input",
                ),
            )
            candidates, rejected = compiler.compile_drafts_with_rejections([draft])
            assert len(candidates) == 1 and not rejected
            step = candidates[0].plan_step
            assert step.arguments["asset_ref"] == asset
            proposal = ActionProposal(
                action_id=ActionId(f"action-{name}"),
                run_id=RunId("run-test"),
                engagement_id=engagement.engagement_id,
                agent_id=draft.agent_id,
                capability="analysis",
                operation=operation,
                target=target,
                arguments=step.arguments,
                reason=step.reason,
                created_at=datetime.now(timezone.utc),
            )
            decision = await control.evaluate(
                proposal,
                ControlContext(
                    engagement_id=engagement.engagement_id,
                ),
            )
            if operation == "file_write":
                assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
                with pytest.raises(NonAllowDecisionError):
                    build_tool_invocation(proposal, decision, tool_id=step.tool_id)
            else:
                assert decision.verdict == PolicyVerdict.ALLOW
                result = await gateway.invoke(
                    build_tool_invocation(
                        proposal,
                        decision,
                        tool_id=step.tool_id,
                    )
                )
                assert result.success, result.error
                # Both assets are in scope, but swapping between them is still forbidden.
                wrong = "vdb-test" if asset == "workspace-test" else "workspace-test"
                altered = draft.model_copy(deep=True)
                altered.plan_step.arguments["asset_ref"] = wrong
                assert compiler.compile_drafts([altered]) == []
                proposal.arguments["asset_ref"] = wrong
                denied = await control.evaluate(
                    proposal,
                    ControlContext(
                        engagement_id=engagement.engagement_id,
                    ),
                )
                assert denied.verdict == PolicyVerdict.DENY
                with pytest.raises(NonAllowDecisionError):
                    build_tool_invocation(proposal, denied, tool_id=step.tool_id)
        assert not (tmp_path / "workspace/must-not-exist.txt").exists()
