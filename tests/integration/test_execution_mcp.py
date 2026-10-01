"""Execution proposal: registry, candidate, Control, Gateway, and MCP integration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.attack_graph.identity import node_id_for_target
from redteam.capability.adapters.mcp.execution import ExecutionMCPAdapter
from redteam.capability.adapters.mcp.transport import RemoteCallResult, RemoteTool
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.execution_manifest import (
    EXECUTION_TOOLS,
    validate_execution_descriptor,
)
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.servers.execution import (
    DockerExecutionEngine,
    ExecutionPolicy,
    create_server,
)
from redteam.capability.workspace_manifest import workspace_specs
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


class CompletedProcess:
    def __init__(self, output: bytes = b"2 input.txt\n") -> None:
        self.returncode: int | None = 0
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


class InProcessTransport:
    def __init__(self, server: Any) -> None:
        self.server = server

    async def list_tools(self) -> list[RemoteTool]:
        async with Client(self.server) as client:
            listing = await client.list_tools()
        return [
            RemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
                output_schema=dict(tool.output_schema or {}),
            )
            for tool in listing.tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        async with Client(self.server) as client:
            result = await client.call_tool(tool_name, arguments)
        output = (
            dict(result.structured_content)
            if result.structured_content is not None
            else None
        )
        return RemoteCallResult(is_error=bool(result.is_error), output=output)

    async def health_check(self) -> bool:
        return True


def policy(tmp_path: Path, *, writable: bool = False) -> ExecutionPolicy:
    return ExecutionPolicy(
        asset_ref="workspace-test",
        engagement_id="eng-test",
        run_id="run-test",
        workspace_root=str(tmp_path.resolve()),
        image="example/text-tools@sha256:" + "1" * 64,
        allowed_executables={"wc": "/usr/bin/wc", "sort": "/usr/bin/sort"},
        writable=writable,
    )


def invocation(arguments: dict[str, Any]) -> ToolInvocation:
    run_id = RunId("run-test")
    return ToolInvocation(
        invocation_id=InvocationId("inv-execution"),
        action_id=ActionId("action-execution"),
        run_id=run_id,
        tool_id=ToolId("analysis.run_command"),
        operation="command_run",
        arguments=arguments,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"),
            run_id=run_id,
        ),
        created_at=datetime.now(timezone.utc),
    )


def job_invocation(
    name: str,
    arguments: dict[str, Any],
    *,
    action_id: str,
) -> ToolInvocation:
    run_id = RunId("run-test")
    return ToolInvocation(
        invocation_id=InvocationId(f"inv-{action_id}"),
        action_id=ActionId(action_id),
        run_id=run_id,
        tool_id=ToolId(f"analysis.{name}"),
        operation=EXECUTION_TOOLS[name].operation,
        arguments=arguments,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"),
            run_id=run_id,
        ),
        created_at=datetime.now(timezone.utc),
    )


def workspace_invocation(name: str, arguments: dict[str, Any]) -> ToolInvocation:
    run_id = RunId("run-test")
    profile = "workspace_write" if name == "write_file" else "workspace_read"
    return ToolInvocation(
        invocation_id=InvocationId(f"inv-{name}"),
        action_id=ActionId(f"action-{name}"),
        run_id=run_id,
        tool_id=ToolId(f"analysis.{name}"),
        operation=workspace_specs(profile)[name].operation,
        arguments={"asset_ref": "workspace-test", **arguments},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"),
            run_id=run_id,
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_registry_candidate_control_gateway_and_real_mcp_tool(
    tmp_path: Path,
) -> None:
    starts = 0

    async def spawn(*args: Any, **kwargs: Any) -> CompletedProcess:
        nonlocal starts
        starts += 1
        return CompletedProcess()

    execution_policy = policy(tmp_path)
    server = create_server(
        execution_policy,
        engine=DockerExecutionEngine(execution_policy, process_factory=spawn),
    )
    adapter = ExecutionMCPAdapter(
        server_id="execution",
        risk_level=RiskLevel.HIGH,
        transport=InProcessTransport(server),
    )
    descriptors = await adapter.discover_tools()
    assert {item.tool_name for item in descriptors} == set(EXECUTION_TOOLS)
    for item in descriptors:
        item.operations = [EXECUTION_TOOLS[item.tool_name].operation]
        item.target_types = ["asset"]
        validate_execution_descriptor(item, "workspace-test")
    registry = MCPToolRegistry()
    registry.replace_server_tools("execution", descriptors)
    gateway = MCPToolGateway(
        registry,
        {"execution": adapter},
        {
            f"analysis.{name}": (spec.operation,)
            for name, spec in EXECUTION_TOOLS.items()
        },
        output_schemas={str(item.tool_id): item.output_schema for item in descriptors},
        target_types={str(item.tool_id): "asset" for item in descriptors},
        health_refresh_enabled=False,
    )

    target = AssetTarget(asset_id=AssetId("workspace-test"))
    draft = CandidateDraft(
        agent_id=AgentId("execution-agent"),
        target_node_ids=[node_id_for_target(target)],
        plan_step=PlanStep(
            step_id="command-run",
            capability="analysis",
            operation="command_run",
            tool_id=ToolId("analysis.run_command"),
            target=target,
            arguments={"executable": "wc", "args": ["-l", "input.txt"]},
            reason="process a synthetic workspace file",
            assessment_kind="observation",
        ),
    )
    compiled, rejected = CandidateCompiler(gateway).compile_drafts_with_rejections([draft])
    assert len(compiled) == 1 and not rejected
    assert compiled[0].plan_step.arguments["asset_ref"] == "workspace-test"

    engagement = Engagement(
        engagement_id=EngagementId("eng-test"),
        name="synthetic execution workspace",
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
    step = compiled[0].plan_step
    proposal = ActionProposal(
        action_id=ActionId("action-execution"),
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
        proposal,
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    with pytest.raises(NonAllowDecisionError):
        build_tool_invocation(proposal, decision, tool_id=step.tool_id)
    assert starts == 0

    await plane.resolve_approval(
        decision.approval_id,
        ApprovalStatus.APPROVED,
        "fixture-operator",
    )
    allowed = await plane.approval_outcome(decision.approval_id)
    result = await gateway.invoke(
        build_tool_invocation(proposal, allowed, tool_id=step.tool_id)
    )
    assert result.success and result.output["exit_code"] == 0
    assert starts == 1

    started = await gateway.invoke(
        job_invocation(
            "job_start",
            {
                "asset_ref": "workspace-test",
                "executable": "wc",
                "args": ["input.txt"],
            },
            action_id="action-job-start",
        )
    )
    assert started.success and started.output["job_id"].startswith("job-")
    job_id = started.output["job_id"]
    for index in range(20):
        status = await gateway.invoke(
            job_invocation(
                "job_status",
                {"asset_ref": "workspace-test", "job_id": job_id},
                action_id=f"action-job-status-{index}",
            )
        )
        assert status.success
        if status.output["status"] != "running":
            break
        await asyncio.sleep(0)
    assert status.output["status"] == "completed"
    cancelled = await gateway.invoke(
        job_invocation(
            "job_cancel",
            {"asset_ref": "workspace-test", "job_id": job_id},
            action_id="action-job-cancel",
        )
    )
    assert cancelled.success
    assert cancelled.output == {
        "job_id": job_id,
        "status": "completed",
        "cancelled": False,
    }

    denied_proposal = proposal.model_copy(deep=True)
    denied_proposal.action_id = ActionId("action-denied")
    denied_proposal.target = AssetTarget(asset_id=AssetId("workspace-other"))
    denied_proposal.arguments["asset_ref"] = "workspace-other"
    denied = await plane.evaluate(
        denied_proposal,
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert denied.verdict == PolicyVerdict.DENY
    with pytest.raises(NonAllowDecisionError):
        build_tool_invocation(denied_proposal, denied, tool_id=step.tool_id)
    assert starts == 2


@pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1",
    reason="set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker",
)
async def test_real_stdio_docker_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = os.environ["REDTEAM_TEST_EXECUTION_IMAGE"]
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_text("b\na\n", encoding="utf-8")
    execution_policy = policy(workspace, writable=True).model_copy(
        update={
            "image": image,
            "container_user": f"{os.getuid()}:{os.getgid()}",
        }
    )
    policy_path = tmp_path / "execution.json"
    policy_path.write_text(
        json.dumps(execution_policy.model_dump()),
        encoding="utf-8",
    )
    workspace_policy_path = tmp_path / "workspace.json"
    workspace_policy_path.write_text(
        json.dumps(
            {
                "asset_ref": "workspace-test",
                "engagement_id": "eng-test",
                "run_id": "run-test",
                "root": str(workspace),
                "writable": True,
            }
        ),
        encoding="utf-8",
    )
    prefix = "REDTEAM_MCP_EXECUTION"
    for suffix, value in {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": sys.executable,
        "PROFILE": "execution",
        "RISK_LEVEL": "high",
        "EXECUTION_POLICY": str(policy_path),
    }.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)

    for server_id, mode in (
        ("workspace-read-test", "read"),
        ("workspace-write-test", "write"),
    ):
        profile = f"workspace_{mode}"
        specs = workspace_specs(profile)
        workspace_prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
        for suffix, value in {
            "KIND": "analysis",
            "TRANSPORT": "stdio",
            "COMMAND": sys.executable,
            "ARGS_JSON": json.dumps(
                [
                    "-m",
                    "redteam.capability.servers.workspace",
                    "--policy",
                    str(workspace_policy_path),
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
        }.items():
            monkeypatch.setenv(f"{workspace_prefix}_{suffix}", value)

    async with open_mcp_tool_gateway(
        ["workspace-read-test", "workspace-write-test", "execution"],
        health_refresh_enabled=False,
    ) as gateway:
        tool = gateway.get(ToolId("analysis.run_command"))
        validate_execution_descriptor(tool, "workspace-test")
        listed = await gateway.invoke(workspace_invocation("list_directory", {}))
        before = await gateway.invoke(
            workspace_invocation("read_file", {"path": "input.txt"})
        )
        result = await gateway.invoke(
            invocation(
                {
                    "asset_ref": "workspace-test",
                    "executable": "sort",
                    "args": ["input.txt", "-o", "result.txt"],
                }
            )
        )
        after = await gateway.invoke(
            workspace_invocation("read_file", {"path": "result.txt"})
        )
    assert listed.success and before.output["content"] == "b\na\n"
    assert result.success
    assert result.output["exit_code"] == 0
    assert after.success and after.output["content"] == "a\nb\n"
