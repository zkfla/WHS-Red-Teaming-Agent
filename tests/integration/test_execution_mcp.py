"""Execution proposal: registry, candidate, Control, Gateway, and MCP integration."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
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
        allowed_executables={
            "cut": "/usr/bin/cut",
            "grep": "/usr/bin/grep",
            "head": "/usr/bin/head",
            "jq": "/usr/bin/jq",
            "wc": "/usr/bin/wc",
            "sort": "/usr/bin/sort",
            "tail": "/usr/bin/tail",
            "uniq": "/usr/bin/uniq",
        },
        writable=writable,
    )


def invocation(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float = 30,
    run_id_value: str = "run-test",
) -> ToolInvocation:
    run_id = RunId(run_id_value)
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
        timeout_seconds=timeout_seconds,
    )


def job_invocation(
    name: str,
    arguments: dict[str, Any],
    *,
    action_id: str,
    run_id_value: str = "run-test",
) -> ToolInvocation:
    run_id = RunId(run_id_value)
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


_DOCKER_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "LANG": os.environ.get("LANG", "C.UTF-8"),
}


def managed_container_ids() -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=io.whs-redteam.execution.managed=true",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=_DOCKER_ENV,
    )
    return result.stdout.split()


async def wait_for_managed_containers(count: int) -> list[str]:
    for _ in range(100):
        identifiers = managed_container_ids()
        if len(identifiers) == count:
            return identifiers
        await asyncio.sleep(0.05)
    pytest.fail(f"expected {count} managed execution containers")


def configure_live_execution(
    monkeypatch: pytest.MonkeyPatch,
    policy_path: Path,
) -> None:
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


def configure_live_workspace(
    monkeypatch: pytest.MonkeyPatch,
    workspace_policy_path: Path,
) -> None:
    for server_id, mode in (
        ("workspace-read-test", "read"),
        ("workspace-write-test", "write"),
    ):
        profile = f"workspace_{mode}"
        specs = workspace_specs(profile)
        prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
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
            monkeypatch.setenv(f"{prefix}_{suffix}", value)


@pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1",
    reason="set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker",
)
async def test_real_stdio_docker_execution_hardening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = os.environ["REDTEAM_TEST_EXECUTION_IMAGE"]
    network_probe_image = os.environ["REDTEAM_TEST_EXECUTION_NETWORK_IMAGE"]
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_text("b\na\n", encoding="utf-8")
    (workspace / "large.txt").write_bytes(b"output-line\n" * 200_000)
    (workspace / "huge.json").write_bytes(b"{}\n" * 2_000_000)
    execution_policy = policy(workspace).model_copy(
        update={
            "image": image,
            "container_user": f"{os.getuid()}:{os.getgid()}",
            "max_output_bytes": 1024,
            "memory": "64m",
        }
    )
    policy_path = tmp_path / "execution.json"
    policy_path.write_text(json.dumps(execution_policy.model_dump()), encoding="utf-8")
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
    configure_live_execution(monkeypatch, policy_path)
    configure_live_workspace(monkeypatch, workspace_policy_path)
    assert await wait_for_managed_containers(0) == []

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
        sorted_result = await gateway.invoke(
            invocation(
                {
                    "asset_ref": "workspace-test",
                    "executable": "sort",
                    "args": ["input.txt"],
                }
            )
        )
        written = await gateway.invoke(
            workspace_invocation(
                "write_file",
                {"path": "result.txt", "content": sorted_result.output["stdout"]},
            )
        )
        after = await gateway.invoke(
            workspace_invocation("read_file", {"path": "result.txt"})
        )

        rejection_cases = [
            (
                invocation(
                    {
                        "asset_ref": "workspace-test",
                        "executable": "python",
                        "args": ["input.txt"],
                    }
                ),
                "execution_executable_not_allowed",
            ),
            (
                invocation(
                    {
                        "asset_ref": "workspace-test",
                        "executable": "grep",
                        "args": ["needle;cat", "input.txt"],
                    }
                ),
                "execution_shell_syntax_forbidden",
            ),
            (
                invocation(
                    {
                        "asset_ref": "workspace-test",
                        "executable": "grep",
                        "args": ["needle", "../outside.txt"],
                    }
                ),
                "execution_invalid_path",
            ),
            (
                invocation(
                    {
                        "asset_ref": "workspace-test",
                        "executable": "sort",
                        "args": ["-o", "result.txt", "input.txt"],
                    }
                ),
                "execution_dangerous_option_forbidden",
            ),
        ]
        for rejected_invocation, expected_code in rejection_cases:
            rejected = await gateway.invoke(rejected_invocation)
            assert rejected.success is False
            assert rejected.error is not None and rejected.error.code == expected_code
            assert managed_container_ids() == []

        stdout_limited = await gateway.invoke(
            invocation(
                {
                    "asset_ref": "workspace-test",
                    "executable": "head",
                    "args": ["-c", "1048576", "large.txt"],
                }
            )
        )
        missing_files = [f"missing-{index:02}.txt" for index in range(63)]
        stderr_limited = await gateway.invoke(
            invocation(
                {
                    "asset_ref": "workspace-test",
                    "executable": "grep",
                    "args": ["needle", *missing_files],
                }
            )
        )
        started = await gateway.invoke(
            job_invocation(
                "job_start",
                {
                    "asset_ref": "workspace-test",
                    "executable": "tail",
                    "args": ["-f", "input.txt"],
                    "timeout_seconds": 10,
                },
                action_id="action-live-job-start",
            )
        )
        assert started.success, started.error
        await asyncio.sleep(0.2)
        live_status = await gateway.invoke(
            job_invocation(
                "job_status",
                {
                    "asset_ref": "workspace-test",
                    "job_id": started.output["job_id"],
                },
                action_id="action-live-job-status",
            )
        )
        assert live_status.success, live_status.error
        assert live_status.output["status"] == "running", live_status.output
        container_id = (await wait_for_managed_containers(1))[0]
        inspected = json.loads(
            subprocess.run(
                ["docker", "inspect", container_id],
                capture_output=True,
                text=True,
                check=True,
                env=_DOCKER_ENV,
            ).stdout
        )[0]
        assert inspected["HostConfig"]["NetworkMode"] == "none"
        assert inspected["HostConfig"]["Memory"] == 64 * 1024 * 1024
        assert inspected["HostConfig"]["NanoCpus"] == 500_000_000
        assert inspected["HostConfig"]["PidsLimit"] == 64
        assert inspected["Config"]["Labels"]["io.whs-redteam.execution.managed"] == "true"

        job_id = started.output["job_id"]
        cross_asset = await gateway.invoke(
            job_invocation(
                "job_status",
                {"asset_ref": "workspace-other", "job_id": job_id},
                action_id="action-cross-asset-status",
            )
        )
        cross_run = await gateway.invoke(
            job_invocation(
                "job_cancel",
                {"asset_ref": "workspace-test", "job_id": job_id},
                action_id="action-cross-run-cancel",
                run_id_value="run-other",
            )
        )
        assert cross_asset.success is False
        assert cross_asset.error is not None
        assert cross_asset.error.code == "execution_invalid_arguments"
        assert cross_run.success is False
        assert cross_run.error is not None
        assert cross_run.error.code == "execution_context_mismatch"
        assert managed_container_ids() == [container_id]

        cancelled = await gateway.invoke(
            job_invocation(
                "job_cancel",
                {"asset_ref": "workspace-test", "job_id": job_id},
                action_id="action-live-job-cancel",
            )
        )
        server_timed_out = await gateway.invoke(
            invocation(
                {
                    "asset_ref": "workspace-test",
                    "executable": "tail",
                    "args": ["-f", "input.txt"],
                    "timeout_seconds": 0.2,
                }
            )
        )
        assert await wait_for_managed_containers(0) == []
        gateway_timed_out = await gateway.invoke(
            invocation(
                {
                    "asset_ref": "workspace-test",
                    "executable": "tail",
                    "args": ["-f", "input.txt"],
                    "timeout_seconds": 10,
                },
                timeout_seconds=0.2,
            )
        )
        assert await wait_for_managed_containers(0) == []

    assert listed.success and before.output["content"] == "b\na\n"
    assert sorted_result.success and sorted_result.output["exit_code"] == 0
    assert written.success and after.output["content"] == "a\nb\n"
    assert stdout_limited.success
    assert len(stdout_limited.output["stdout"].encode()) == 1024
    assert stdout_limited.output["truncated"] is True
    assert stderr_limited.success
    assert len(stderr_limited.output["stderr"].encode()) == 1024
    assert stderr_limited.output["truncated"] is True
    assert server_timed_out.success and server_timed_out.output["timed_out"] is True
    assert gateway_timed_out.success is False
    assert gateway_timed_out.error is not None
    assert gateway_timed_out.error.code == "tool_timeout"
    assert cancelled.success and cancelled.output["status"] == "cancelled"
    assert await wait_for_managed_containers(0) == []

    async with open_mcp_tool_gateway(
        ["execution"],
        health_refresh_enabled=False,
    ) as cancellation_gateway:
        cancel_task = asyncio.create_task(
            cancellation_gateway.invoke(
                invocation(
                    {
                        "asset_ref": "workspace-test",
                        "executable": "tail",
                        "args": ["-f", "input.txt"],
                        "timeout_seconds": 10,
                    }
                )
            )
        )
        await wait_for_managed_containers(1)
        cancel_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancel_task
        assert await wait_for_managed_containers(0) == []

    direct_engine = DockerExecutionEngine(execution_policy)
    direct_output = await direct_engine.run_command(
        "workspace-test",
        "head",
        ["-c", "1048576", "large.txt"],
        10,
        "eng-test",
        "run-test",
    )
    assert direct_output.exit_code == 0 and direct_output.truncated is True
    assert direct_engine.last_peak_output_bytes == execution_policy.max_output_bytes

    probe_values = execution_policy.model_dump()
    probe_values.update(
        image=network_probe_image,
        allowed_executables={"python": "/usr/local/bin/python"},
        allowed_options={"python": ("-c",)},
    )
    network_probe_policy = ExecutionPolicy.model_construct(**probe_values)
    network_probe = await DockerExecutionEngine(network_probe_policy).run_command(
        "workspace-test",
        "python",
        ["-c", "__import__('socket').create_connection(('1.1.1.1',53),1)"],
        5,
        "eng-test",
        "run-test",
    )
    assert network_probe.exit_code not in (None, 0)
    assert network_probe.timed_out is False

    oom_policy = execution_policy.model_copy(update={"memory": "16m"})
    oom_result = await DockerExecutionEngine(oom_policy).run_command(
        "workspace-test",
        "jq",
        ["-s", ".", "huge.json"],
        20,
        "eng-test",
        "run-test",
    )
    assert oom_result.exit_code not in (None, 0)
    assert oom_result.timed_out is False
    assert await wait_for_managed_containers(0) == []


@pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1",
    reason="set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker",
)
async def test_stdio_server_restart_cleans_fault_injected_orphan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_text("line\n", encoding="utf-8")
    execution_policy = policy(workspace).model_copy(
        update={
            "image": os.environ["REDTEAM_TEST_EXECUTION_IMAGE"],
            "container_user": f"{os.getuid()}:{os.getgid()}",
        }
    )
    policy_path = tmp_path / "fault-policy.json"
    policy_path.write_text(json.dumps(execution_policy.model_dump()), encoding="utf-8")
    configure_live_execution(monkeypatch, policy_path)
    assert await wait_for_managed_containers(0) == []

    marker = tmp_path / "fault-client-ready"
    client_log = tmp_path / "fault-client.log"
    helper = Path(__file__).parents[1] / "fakes" / "execution_fault_client.py"
    with client_log.open("wb") as output:
        client = subprocess.Popen(
            [sys.executable, str(helper), str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            for _ in range(200):
                if client.poll() is not None:
                    pytest.fail(client_log.read_text(encoding="utf-8", errors="replace"))
                if marker.exists() and managed_container_ids():
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("fault client did not start a managed container")

            processes = subprocess.run(
                ["ps", "-ww", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            server_pids = [
                int(line.strip().split(maxsplit=1)[0])
                for line in processes
                if "redteam.capability.servers.execution" in line
                and str(policy_path) in line
            ]
            execution_processes = [
                line
                for line in processes
                if "redteam.capability.servers.execution" in line
            ]
            assert len(server_pids) == 1, "\n".join(execution_processes)
            os.kill(server_pids[0], signal.SIGKILL)
            await asyncio.sleep(0.2)
        finally:
            if client.poll() is None:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait(timeout=5)

    async with open_mcp_tool_gateway(
        ["execution"],
        health_refresh_enabled=False,
    ):
        assert await wait_for_managed_containers(0) == []
    assert await wait_for_managed_containers(0) == []
