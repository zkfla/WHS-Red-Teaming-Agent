"""Real stdio discovery, registry contracts and approved workspace IO."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.attack_graph.identity import node_id_for_target
from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.capability.workspace_manifest import (
    validate_workspace_descriptor,
    workspace_specs,
)
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
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.convert import NonAllowDecisionError, build_tool_invocation
from redteam.core.enums import ApprovalStatus, PolicyVerdict, RiskLevel
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, InvocationId, RunId, ToolId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft


def configure_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    writable: bool = True,
    limits: dict | None = None,
) -> list[str]:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    (root / "source.txt").write_text("synthetic marker\n금융 시험", encoding="utf-8")
    policy = tmp_path / "workspace-policy.json"
    policy.write_text(
        json.dumps(
            {
                "root": str(root),
                "asset_ref": "workspace-test",
                "engagement_id": "eng-test",
                "run_id": "run-test",
                "writable": writable,
                **(limits or {}),
            }
        ),
        encoding="utf-8",
    )
    servers = (
        ["workspace-read-test", "workspace-write-test"] if writable else ["workspace-read-test"]
    )
    for server_id in servers:
        mode = "write" if "write" in server_id else "read"
        profile = f"workspace_{mode}"
        specs = workspace_specs(profile)
        prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
        values = {
            "KIND": "analysis",
            "TRANSPORT": "stdio",
            "COMMAND": sys.executable,
            "ARGS_JSON": json.dumps(
                [
                    "-m",
                    "redteam.capability.servers.workspace",
                    "--policy",
                    str(policy),
                    "--mode",
                    mode,
                ]
            ),
            "PROFILE": profile,
            "WORKSPACE_ASSET_REF": "workspace-test",
            "RISK_LEVEL": "high" if mode == "write" else "medium",
            "ALLOWED_TOOLS_JSON": json.dumps(list(specs)),
            "TOOL_OPERATIONS_JSON": json.dumps(
                {name: [spec.operation] for name, spec in specs.items()}
            ),
        }
        for suffix, value in values.items():
            monkeypatch.setenv(f"{prefix}_{suffix}", value)
    return servers


def invocation(name: str, args: dict, **changes) -> ToolInvocation:
    profile = "workspace_write" if name in {"write_file", "apply_patch"} else "workspace_read"
    return ToolInvocation(
        invocation_id=InvocationId("inv-workspace"),
        action_id=ActionId("action-workspace"),
        run_id=RunId("run-test"),
        tool_id=ToolId(f"analysis.{name}"),
        operation=workspace_specs(profile)[name].operation,
        arguments={"asset_ref": "workspace-test", **args},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"),
            run_id=RunId("run-test"),
        ),
        created_at=datetime.now(timezone.utc),
        **changes,
    )


async def test_real_stdio_manifest_and_file_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    servers = configure_workspace(monkeypatch, tmp_path)
    async with open_mcp_tool_gateway(servers, health_refresh_enabled=False) as gateway:
        catalog = gateway.list_tools_for_action("analysis", "file_read")
        assert [str(tool.tool_id) for tool in catalog] == [
            build_canonical_mcp_tool_id("analysis", "workspace-read-test", "read_file")
        ]
        for mode in ("read", "write"):
            profile = f"workspace_{mode}"
            for name, spec in workspace_specs(profile).items():
                tool = gateway.get(ToolId(f"analysis.{name}"))
                assert tool is not None
                validate_workspace_descriptor(tool, profile, "workspace-test")
                assert set(tool.required_arguments) == spec.required
                assert tool.risk_level == (RiskLevel.HIGH if mode == "write" else RiskLevel.MEDIUM)
                assert tool.target_types == ["asset"]
        calls = [
            invocation("list_directory", {}),
            invocation("read_file", {"path": "source.txt"}),
            invocation("search_files", {"pattern": "marker"}),
            invocation("write_file", {"path": "notes.md", "content": "draft marker"}),
        ]
        results = [await gateway.invoke(call) for call in calls]
        assert all(result.success for result in results), results
        assert isinstance(results[1].output, dict)
        assert isinstance(results[2].output, dict)
        assert isinstance(results[3].output, dict)
        assert results[1].output["content"] == "synthetic marker\n금융 시험"
        assert results[2].output["matches"][0]["path"] == "source.txt"
        assert "run_id" not in calls[0].arguments  # context injection never mutates the proposal
        digest = results[3].output["sha256"]
        patched = await gateway.invoke(
            invocation(
                "apply_patch",
                {
                    "path": "notes.md",
                    "old_text": "draft",
                    "new_text": "reviewed",
                    "expected_sha256": digest,
                },
            )
        )
        assert patched.success, patched
        reread = await gateway.invoke(invocation("read_file", {"path": "notes.md"}))
        assert isinstance(reread.output, dict)
        assert reread.success and reread.output["content"] == "reviewed marker"
        stale = await gateway.invoke(
            invocation(
                "write_file",
                {
                    "path": "notes.md",
                    "content": "overwrite",
                    "expected_sha256": digest,
                },
            )
        )
        assert not stale.success


async def test_real_stdio_context_and_operation_rejections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    servers = configure_workspace(monkeypatch, tmp_path, writable=False)
    async with open_mcp_tool_gateway(servers, health_refresh_enabled=False) as gateway:
        assert gateway.get(ToolId("analysis.write_file")) is None
        for field in ("run_id", "engagement_id"):
            bad = await gateway.invoke(
                invocation("read_file", {"path": "source.txt", field: "other"})
            )
            assert bad.error is not None
            assert not bad.success and bad.error.code == "workspace_context_mismatch"
        forged_context = invocation("read_file", {"path": "source.txt"})
        forged_context.execution_context.engagement_id = EngagementId("other-engagement")
        refused = await gateway.invoke(forged_context)
        assert refused.error is not None
        assert not refused.success and refused.error.code == "mcp_tool_error"
        wrong_run = invocation("read_file", {"path": "source.txt"})
        wrong_run.run_id = RunId("other-run")
        wrong_run_result = await gateway.invoke(wrong_run)
        assert wrong_run_result.error is not None
        assert wrong_run_result.error.code == "workspace_context_mismatch"
        bad_operation = invocation("read_file", {"path": "source.txt"})
        bad_operation.operation = "file_write"
        assert not (await gateway.invoke(bad_operation)).success
        for args in ({"path": "../policy.json"}, {"asset_ref": "other", "path": "source.txt"}):
            assert not (await gateway.invoke(invocation("read_file", args))).success
        for invalid_args in ({"path": 123}, {"path": "source.txt", "shell": "no"}):
            bad = await gateway.invoke(invocation("read_file", invalid_args))
            assert bad.error is not None
            assert not bad.success and bad.error.code == "workspace_invalid_arguments"


async def test_candidate_compiler_control_approval_and_real_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    servers = configure_workspace(monkeypatch, tmp_path)
    target = AssetTarget(asset_id=AssetId("workspace-test"))
    async with open_mcp_tool_gateway(servers, health_refresh_enabled=False) as gateway:
        engagement = Engagement(
            engagement_id=EngagementId("eng-test"),
            name="synthetic workspace",
            scope=EngagementScope(allowed_assets=["workspace-test"]),
            execution_budget=ExecutionBudget(max_actions=10),
            rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
            approval_policy=ApprovalPolicy(),
            created_at=datetime.now(timezone.utc),
        )
        plane = DefaultControlPlane(
            {engagement.engagement_id: engagement},
            risk_resolver=RegistryRiskResolver(gateway),
        )
        for name, args in (
            ("read_file", {"path": "source.txt"}),
            ("write_file", {"path": "approved.md", "content": "synthetic"}),
        ):
            profile = "workspace_read" if name == "read_file" else "workspace_write"
            draft = CandidateDraft(
                agent_id=AgentId("workspace-agent"),
                target_node_ids=[node_id_for_target(target)],
                plan_step=PlanStep(
                    step_id=name,
                    capability="analysis",
                    operation=workspace_specs(profile)[name].operation,
                    tool_id=ToolId(f"analysis.{name}"),
                    target=target,
                    arguments=args,
                    reason="approved synthetic workspace task",
                    assessment_kind="observation",
                ),
            )
            compiled, rejected = CandidateCompiler(gateway).compile_drafts_with_rejections([draft])
            assert len(compiled) == 1 and not rejected
            step = compiled[0].plan_step
            assert step.arguments["asset_ref"] == "workspace-test"
            proposal = ActionProposal(
                action_id=ActionId(f"action-{name}"),
                run_id=RunId("run-test"),
                engagement_id=engagement.engagement_id,
                agent_id=draft.agent_id,
                capability=step.capability,
                operation=step.operation,
                target=step.resolved_target(),
                arguments=step.arguments,
                reason=step.reason,
                created_at=datetime.now(timezone.utc),
            )
            decision = await plane.evaluate(
                proposal, ControlContext(engagement_id=engagement.engagement_id)
            )
            if name == "write_file":
                assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
                with pytest.raises(NonAllowDecisionError):
                    build_tool_invocation(proposal, decision, tool_id=step.tool_id)
                assert not (tmp_path / "workspace" / "approved.md").exists()
                # Simulated human response for this synthetic fixture, NOT production auto-approval.
                assert decision.approval_id is not None
                await plane.resolve_approval(
                    decision.approval_id, ApprovalStatus.APPROVED, "fixture-operator"
                )
                outcome = await plane.approval_outcome(decision.approval_id)
                assert outcome is not None
                decision = outcome
            assert decision.verdict == PolicyVerdict.ALLOW
            result = await gateway.invoke(
                build_tool_invocation(proposal, decision, tool_id=step.tool_id)
            )
            assert result.success, result
        proposal.arguments["asset_ref"] = "different-workspace"
        refused = await plane.evaluate(
            proposal, ControlContext(engagement_id=engagement.engagement_id)
        )
        assert refused.verdict == PolicyVerdict.DENY
        with pytest.raises(NonAllowDecisionError):
            build_tool_invocation(proposal, refused, tool_id=step.tool_id)


async def test_registry_manifest_rejects_mutated_input_and_output_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    servers = configure_workspace(monkeypatch, tmp_path, writable=False)
    async with open_mcp_tool_gateway(servers, health_refresh_enabled=False) as gateway:
        original = gateway.get(ToolId("analysis.read_file"))
        assert original is not None
        for part in ("input", "required", "output", "operation"):
            changed = original.model_copy(deep=True)
            if part == "input":
                changed.input_schema["properties"]["path"]["type"] = "integer"
            elif part == "required":
                changed.input_schema["required"].remove("path")
            elif part == "output":
                assert changed.output_schema is not None
                changed.output_schema["properties"].pop("sha256")
            else:
                changed.operations = ["http_request"]
            with pytest.raises(ValueError, match="manifest"):
                validate_workspace_descriptor(changed, "workspace_read", "workspace-test")


async def test_read_default_and_discovery_follow_operator_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    servers = configure_workspace(
        monkeypatch,
        tmp_path,
        writable=False,
        limits={"max_return_chars": 4, "max_file_bytes": 16},
    )
    (tmp_path / "workspace" / "source.txt").write_text("가나다라마", encoding="utf-8")
    async with open_mcp_tool_gateway(servers, health_refresh_enabled=False) as gateway:
        tool = gateway.get(ToolId("analysis.read_file"))
        assert tool is not None
        assert tool.input_schema["properties"]["max_chars"]["default"] == 4
        assert tool.input_schema["properties"]["max_chars"]["maximum"] == 4
        result = await gateway.invoke(invocation("read_file", {"path": "source.txt"}))
        assert isinstance(result.output, dict)
        assert result.success and result.output["content"] == "가나다라"
        assert result.output["next_offset_chars"] == 4 and result.output["size_bytes"] == 15
        bad = await gateway.invoke(invocation("read_file", {"path": "source.txt", "max_chars": 5}))
        assert bad.error is not None
        assert not bad.success and bad.error.code == "workspace_invalid_arguments"


def test_write_profile_cannot_downgrade_risk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv("REDTEAM_MCP_WORKSPACE_WRITE_TEST_RISK_LEVEL", "medium")
    with pytest.raises(MCPConfigurationError, match="risk"):
        MCPServerSettings.from_environment("workspace-write-test")


async def test_real_discovery_rejects_changed_workspace_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    servers = configure_workspace(monkeypatch, tmp_path, writable=False)
    monkeypatch.setenv("REDTEAM_MCP_WORKSPACE_READ_TEST_WORKSPACE_ASSET_REF", "different-asset")
    with pytest.RaisesGroup(MCPConfigurationError, flatten_subgroups=True):
        async with open_mcp_tool_gateway(servers, health_refresh_enabled=False):
            pytest.fail("schema mismatch must prevent startup")


@pytest.mark.parametrize(
    "suffix,value",
    [
        ("RISK_LEVEL", "low"),
        ("ALLOWED_TOOLS_JSON", '["read_file"]'),
        ("TOOL_OPERATIONS_JSON", '{"read_file": ["file_write"]}'),
    ],
)
def test_workspace_settings_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
    value: str,
) -> None:
    configure_workspace(monkeypatch, tmp_path, writable=False)
    monkeypatch.setenv(f"REDTEAM_MCP_WORKSPACE_READ_TEST_{suffix}", value)
    with pytest.raises(MCPConfigurationError):
        MCPServerSettings.from_environment("workspace-read-test")
