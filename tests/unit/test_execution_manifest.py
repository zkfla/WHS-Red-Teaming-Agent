from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.capability.adapters.mcp.execution import ExecutionMCPAdapter
from redteam.capability.adapters.mcp.transport import RemoteCallResult
from redteam.capability.execution_manifest import (
    EXECUTION_TOOLS,
    validate_execution_descriptor,
)
from redteam.capability.servers.execution import (
    DockerExecutionEngine,
    ExecutionPolicy,
    create_server,
)
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId


class CompletedProcess:
    returncode = 0

    def __init__(self) -> None:
        import asyncio

        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"ok\n")
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


def make_policy(tmp_path: Path) -> ExecutionPolicy:
    return ExecutionPolicy(
        asset_ref="workspace-test",
        engagement_id="eng-test",
        run_id="run-test",
        workspace_root=str(tmp_path.resolve()),
        image="example/text-tools@sha256:" + "1" * 64,
        allowed_executables={"grep": "/usr/bin/grep"},
    )


def write_policy(tmp_path: Path) -> Path:
    path = tmp_path / "execution-policy.json"
    path.write_text(json.dumps(make_policy(tmp_path).model_dump()), encoding="utf-8")
    return path


def configure(monkeypatch: pytest.MonkeyPatch, policy_path: Path) -> None:
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_KIND", "analysis")
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_COMMAND", sys.executable)
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_PROFILE", "execution")
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_RISK_LEVEL", "high")
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_EXECUTION_POLICY", str(policy_path))


class ErrorTransport:
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        return RemoteCallResult(
            is_error=True,
            output={"error_code": "execution_option_not_allowed"},
        )


def test_execution_profile_pins_policy_tools_operations_and_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = write_policy(tmp_path)
    configure(monkeypatch, policy_path)

    settings = MCPServerSettings.from_environment("execution")

    assert settings.profile == "execution"
    assert settings.risk_level == RiskLevel.HIGH
    assert settings.execution_asset_ref == "workspace-test"
    assert settings.allowed_tools == tuple(EXECUTION_TOOLS)
    assert settings.tool_operations == {
        name: (spec.operation,) for name, spec in EXECUTION_TOOLS.items()
    }
    assert settings.args[:4] == (
        "-m",
        "redteam.capability.servers.execution",
        "--policy",
        str(policy_path.resolve()),
    )
    assert settings.args[4] == "--sha256"
    assert settings.args[5] == settings.execution_policy_sha256


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_execution_profile_cannot_downgrade_risk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    risk: str,
) -> None:
    policy_path = write_policy(tmp_path)
    configure(monkeypatch, policy_path)
    monkeypatch.setenv("REDTEAM_MCP_EXECUTION_RISK_LEVEL", risk)
    with pytest.raises(MCPConfigurationError, match="risk"):
        MCPServerSettings.from_environment("execution")


async def test_real_mcp_discovery_matches_pinned_manifest(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)

    async def spawn(*args: Any, **kwargs: Any) -> CompletedProcess:
        return CompletedProcess()

    server = create_server(policy, engine=DockerExecutionEngine(policy, process_factory=spawn))
    async with Client(server) as client:
        listing = await client.list_tools()
    assert {tool.name for tool in listing.tools} == set(EXECUTION_TOOLS)
    descriptors = []
    for remote in listing.tools:
        spec = EXECUTION_TOOLS[remote.name]
        descriptor = ToolDescriptor(
            tool_id=ToolId(f"analysis.{remote.name}"),
            server_id="execution",
            tool_name=remote.name,
            description=remote.description or "",
            input_schema=dict(remote.input_schema),
            output_schema=dict(remote.output_schema or {}),
            operations=[spec.operation],
            target_types=["asset"],
            capability_tags=["analysis"],
            risk_level=RiskLevel.HIGH,
            health_status=ToolHealthStatus.HEALTHY,
        )
        validate_execution_descriptor(descriptor, "workspace-test")
        descriptors.append(descriptor)

    changed = next(item for item in descriptors if item.tool_name == "run_command").model_copy(
        deep=True
    )
    changed.input_schema["required"].remove("args")
    with pytest.raises(ValueError, match="manifest"):
        validate_execution_descriptor(changed, "workspace-test")


async def test_execution_adapter_preserves_stable_server_error_code() -> None:
    descriptor = ToolDescriptor(
        tool_id=ToolId("analysis.run_command"),
        server_id="execution",
        tool_name="run_command",
        description="proposed execution",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        operations=["command_run"],
        target_types=["asset"],
        capability_tags=["analysis"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )
    result = await ExecutionMCPAdapter(
        server_id="execution",
        risk_level=RiskLevel.HIGH,
        transport=ErrorTransport(),
    ).invoke(descriptor, {})
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "execution_option_not_allowed"


async def test_server_returns_execution_error_as_structured_tool_failure(
    tmp_path: Path,
) -> None:
    policy = make_policy(tmp_path)

    async def fail(*args: Any, **kwargs: Any) -> CompletedProcess:
        pytest.fail("rejected executable must not start Docker")

    server = create_server(
        policy,
        engine=DockerExecutionEngine(policy, process_factory=fail),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "run_command",
            {
                "asset_ref": "workspace-test",
                "executable": "python",
                "args": ["input.txt"],
                "engagement_id": "eng-test",
                "run_id": "run-test",
            },
        )
    assert result.is_error is True
    assert result.structured_content == {
        "error_code": "execution_executable_not_allowed"
    }
