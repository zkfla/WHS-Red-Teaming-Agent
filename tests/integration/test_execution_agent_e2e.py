"""Opt-in Analysis Agent path through Control, Gateway, execution MCP and Evidence."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.app.bootstrap import run_engagement
from redteam.capability.adapters.mcp.execution import ExecutionMCPAdapter
from redteam.capability.adapters.mcp.transport import (
    RemoteCallResult,
    RemoteTool,
    remote_tool_metadata,
)
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
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import AssetTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.enums import EvidenceSourceType, PolicyVerdict, RiskLevel, ToolHealthStatus
from redteam.core.ids import (
    ActionId,
    AgentId,
    AssetId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from tests.fakes import build_test_application
from tests.integration._engine_llm_harness import ScriptedLLM, review_accept

ASSET = "workspace-test"
ASSET_NODE = "asset:workspace-test"
RUN_ID = RunId("run-test")
PATH_STEP = "process-workspace-input"


class CompletedProcess:
    returncode: int | None = 0

    def __init__(self, output: bytes = b"3 input.txt\n") -> None:
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
                metadata=remote_tool_metadata(tool),
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


def policy(workspace: Path, *, image: str | None = None) -> ExecutionPolicy:
    return ExecutionPolicy(
        asset_ref=ASSET,
        engagement_id="eng-test",
        run_id=str(RUN_ID),
        workspace_root=str(workspace.resolve()),
        image=image or "example/text-tools@sha256:" + "1" * 64,
        allowed_executables={"wc": "/usr/bin/wc"},
        container_user=(
            f"{os.getuid()}:{os.getgid()}" if image is not None else "65532:65532"
        ),
    )


async def gateway_for(
    execution_policy: ExecutionPolicy,
    *,
    process_factory: Any | None = None,
) -> MCPToolGateway:
    engine = (
        DockerExecutionEngine(execution_policy)
        if process_factory is None
        else DockerExecutionEngine(execution_policy, process_factory=process_factory)
    )
    adapter = ExecutionMCPAdapter(
        server_id="execution",
        risk_level=RiskLevel.HIGH,
        transport=InProcessTransport(create_server(execution_policy, engine=engine)),
    )
    descriptors = await adapter.discover_tools()
    for descriptor in descriptors:
        descriptor.operations = [EXECUTION_TOOLS[descriptor.tool_name].operation]
        descriptor.target_types = ["asset"]
        validate_execution_descriptor(descriptor, ASSET)
        assert descriptor.output_schema is not None
        assert descriptor.health_status is ToolHealthStatus.HEALTHY
    registry = MCPToolRegistry()
    registry.replace_server_tools("execution", descriptors)
    return MCPToolGateway(
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


def engagement(*, allowed_operation: str = "analysis.command_run") -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng-test"),
        name="execution Agent E2E",
        scope=EngagementScope(allowed_assets=[ASSET]),
        rules_of_engagement=[
            RuleOfEngagement(
                name="execution Agent opt-in",
                allowed_capabilities=["analysis"],
                allowed_operations=[allowed_operation],
            )
        ],
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(auto_approve_up_to=RiskLevel.HIGH),
        created_at=datetime.now(timezone.utc),
        metadata={
            "asset_ref": ASSET,
            "execution_agent": {
                "enabled": True,
                "assets": {
                    ASSET: {"recipe": "line_count", "path": "input.txt"}
                },
            },
        },
    )


def draft_path() -> dict[str, Any]:
    return {
        "path_id": "path-execution-agent",
        "run_id": str(RUN_ID),
        "mission_id": None,
        "version": 1,
        "parent_version": None,
        "status": "draft",
        "reason": "process an approved workspace input",
        "goal": "count lines in the approved input",
        "steps": [
            {
                "step_id": PATH_STEP,
                "objective": "count lines in the approved workspace input",
                "agent_id": "analysis-agent",
                "target_node_ids": [ASSET_NODE],
                "principal_refs": [],
                "allowed_operations": ["analysis.command_run"],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {
                        "kind": "evidence",
                        "description": "command output evidence recorded",
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


def turn_action() -> dict[str, Any]:
    return {
        "mode": "actions",
        "reason": "use the offered fixed execution option",
        "actions": [
            {
                "path_step_id": PATH_STEP,
                "agent_id": "analysis-agent",
                "target_node_id": ASSET_NODE,
                "principal_ref": None,
                "capability": "analysis",
                "operation": "command_run",
                "tool_id": "analysis.run_command",
                # Direct selection supplies the valid registered input; legacy
                # recipe replacement and executable denial have separate tests.
                "arguments": {"executable": "wc", "args": ["-l", "input.txt"]},
                "reason": "process the configured graph-bound input",
                "expected_observation": "bounded line count output",
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


def hold() -> dict[str, Any]:
    return {
        "mode": "hold",
        "reason": "execution path evidence captured",
        "actions": [],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def scripted_llm() -> ScriptedLLM:
    llm = ScriptedLLM()
    llm.add("path_draft", draft_path())
    llm.add("path_review", review_accept())
    llm.add("next_action", turn_action())
    llm.add("next_action", hold())
    return llm


async def run_agent_loop(
    configured: Engagement,
    gateway: MCPToolGateway,
) -> tuple[Any, Any, ScriptedLLM]:
    llm = scripted_llm()
    deps = build_test_application(
        control_plane=DefaultControlPlane(
            {configured.engagement_id: configured},
            risk_resolver=RegistryRiskResolver(gateway),
            outbound_descriptor_resolver=gateway.get,
        ),
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=12, checkpointing=False),
    )
    deps = replace(deps, engagement=configured)
    final = await run_engagement(configured, deps=deps, run_id=RUN_ID)
    return final, deps, llm


async def test_agent_loop_reaches_execution_mcp_and_records_evidence(
    tmp_path: Path,
) -> None:
    starts: list[tuple[str, ...]] = []

    async def spawn(*args: str, **_: Any) -> CompletedProcess:
        starts.append(args)
        return CompletedProcess()

    (tmp_path / "input.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    gateway = await gateway_for(policy(tmp_path), process_factory=spawn)

    final, deps, llm = await run_agent_loop(engagement(), gateway)

    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
    assert final.actions_executed == 1
    assert len(starts) == 1
    docker_argv = starts[0]
    assert "/usr/bin/wc" in docker_argv
    assert docker_argv[-2:] == ("-l", "input.txt")
    assert "python" not in docker_argv
    evidence = await deps.evidence_store.list_for_run(RUN_ID)
    tool_evidence = [item for item in evidence if item.source_type is EvidenceSourceType.TOOL]
    assert len(tool_evidence) == 1
    assert tool_evidence[0].metadata["success"] is True
    path_prompt = json.loads(llm.calls[0]["messages"][0]["content"])
    assert path_prompt["execution_options"][0]["arguments"]["executable"] == "wc"


async def test_roe_exclusion_is_denied_by_control_before_execution(tmp_path: Path) -> None:
    starts = 0

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        nonlocal starts
        starts += 1
        return CompletedProcess()

    configured = engagement(allowed_operation="analysis.static_analysis")
    gateway = await gateway_for(policy(tmp_path), process_factory=spawn)
    proposal = ActionProposal(
        action_id=ActionId("action-roe-denied"),
        run_id=RUN_ID,
        engagement_id=configured.engagement_id,
        agent_id=AgentId("analysis-agent"),
        capability="analysis",
        operation="command_run",
        target=AssetTarget(asset_id=AssetId(ASSET)),
        arguments={"asset_ref": ASSET, "executable": "wc", "args": ["-l", "input.txt"]},
        reason="verify Control denies an operation outside the allowlist",
        created_at=datetime.now(timezone.utc),
    )
    plane = DefaultControlPlane(
        {configured.engagement_id: configured},
        risk_resolver=RegistryRiskResolver(gateway),
    )

    decision = await plane.evaluate(
        proposal, ControlContext(engagement_id=configured.engagement_id)
    )

    assert decision.verdict is PolicyVerdict.DENY
    assert starts == 0


async def test_server_rejects_unallowlisted_executable_without_starting_docker(
    tmp_path: Path,
) -> None:
    starts = 0

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        nonlocal starts
        starts += 1
        return CompletedProcess()

    gateway = await gateway_for(policy(tmp_path), process_factory=spawn)
    result = await gateway.invoke(
        ToolInvocation(
            invocation_id=InvocationId("inv-reject-python"),
            action_id=ActionId("action-reject-python"),
            run_id=RUN_ID,
            tool_id=ToolId("analysis.run_command"),
            operation="command_run",
            arguments={"asset_ref": ASSET, "executable": "python", "args": ["-c", "bad"]},
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng-test"),
                run_id=RUN_ID,
            ),
            created_at=datetime.now(timezone.utc),
        )
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "execution_executable_not_allowed"
    assert starts == 0


@pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1",
    reason="set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker",
)
async def test_agent_loop_executes_real_docker_and_records_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    gateway = await gateway_for(
        policy(workspace, image=os.environ["REDTEAM_TEST_EXECUTION_IMAGE"])
    )

    final, deps, _ = await run_agent_loop(engagement(), gateway)

    assert final.actions_executed == 1
    evidence = await deps.evidence_store.list_for_run(RUN_ID)
    tool_evidence = [item for item in evidence if item.source_type is EvidenceSourceType.TOOL]
    assert len(tool_evidence) == 1
    result = tool_evidence[0].metadata["tool_result"]
    assert result["success"] is True
    assert result["output"]["exit_code"] == 0
    assert result["output"]["stdout"].lstrip().startswith("3 ")
