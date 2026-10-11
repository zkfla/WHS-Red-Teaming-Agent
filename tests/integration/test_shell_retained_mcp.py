"""Shell builtin and a real retained MCP server share one gateway (FR-005).

This file owns exactly one contract: after the builtin shell provider is
registered, the retained web-http MCP server is still discovered and still
invocable through the same gateway, and the shell tool is invocable through
that gateway as well. The MCP half is a real stdio deployment (the approved
deployment harness), never a fake adapter.

Shell execution uses the in-process fake workroom because the real Docker
workroom path is already covered by the shell workroom and native-pipeline
integration tests. The "execution MCP removed" configuration proof is
covered by the shell-native/runtime-008 tests and is not duplicated here.
No production code and no model tool-name branching are involved.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import datetime, timezone

from redteam.app.bootstrap import build_application
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import RUN_TOOL_NAME, SHELL_SERVER_ID
from redteam.core.config import AppConfig, CapabilityConfig, load_engagement_config
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
    new_id,
)
from redteam.core.tool_binding import SELECTED_TOOL
from redteam.orchestration.settings import OrchestrationSettings
from tests.unit.test_mcp_deployment import engagement, source
from tests.unit.test_shell_adapter import _FakeWorkroom, _policy

RUN_ID = "run_shell_retained"
ENGAGEMENT_ID = "eng_shell_retained"
WORKROOM_KEY = "run_shell_retained.inst1"
INSTANCE_ID = "inst1"


class _StubLLM:
    """The contract under test never asks the model for anything."""

    async def complete(self, **_kwargs: object) -> str:
        raise AssertionError("no model call is part of this contract test")

    async def complete_structured(self, **_kwargs: object) -> object:
        raise AssertionError("no model call is part of this contract test")

    async def complete_native(self, **_kwargs: object) -> object:
        raise AssertionError("no model call is part of this contract test")


def _deployed_config(tmp_path, monkeypatch) -> AppConfig:
    spec = DeploymentSpec.model_validate(source("https://127.0.0.1:8443"))
    config = engagement()
    config.approval_policy.require_approval_for = []
    path = provision(spec, config, tmp_path / "retained")
    loaded = load_engagement_config(path)
    binding = loaded.metadata["mcp_deployment"]
    monkeypatch.setenv("TEST_ALICE", "synthetic-a")
    monkeypatch.setenv("TEST_BOB", "synthetic-b")
    for key, value in connection_environment(
        spec, binding["policy_path"], binding["policy_sha256"]
    ).items():
        monkeypatch.setenv(key, value)
    return AppConfig(
        engagement=loaded, capability=CapabilityConfig(mcp_servers=list(spec.roles))
    )


def _invocation(tool_id: str, arguments: dict) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId(new_id("inv")),
        action_id=ActionId(new_id("action")),
        run_id=RunId(RUN_ID),
        tool_id=ToolId(tool_id),
        binding_mode=SELECTED_TOOL,
        arguments=arguments,
        timeout_seconds=60.0,
        execution_context=ExecutionContext(
            engagement_id=EngagementId(ENGAGEMENT_ID),
            run_id=RunId(RUN_ID),
            instance_id=AgentInstanceId(INSTANCE_ID),
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_shell_builtin_and_retained_mcp_share_one_gateway(tmp_path, monkeypatch):
    config = _deployed_config(tmp_path, monkeypatch)
    shell = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    async with AsyncExitStack() as stack:
        _engagement, deps = await build_application(
            config,
            mcp_stack=stack,
            llm=_StubLLM(),
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
            orchestration_settings=OrchestrationSettings(
                max_steps=5,
                checkpoint_backend="memory",
                enforce_verification_reserve=False,
            ),
            builtin_adapters=[shell],
        )
        gateway = deps.tool_gateway
        descriptors = gateway.all_descriptors()
        ids = [str(item.tool_id) for item in descriptors]
        assert len(ids) == len(set(ids))
        # The retained MCP server survives builtin registration.
        mcp_tools = [item for item in descriptors if item.server_id != SHELL_SERVER_ID]
        assert mcp_tools, "the retained MCP server must still be discovered"
        inventory = next(
            (item for item in mcp_tools if item.tool_name == "session_list"), None
        )
        assert inventory is not None, [str(item.tool_id) for item in mcp_tools]
        # The builtin shell provider is registered in the same gateway.
        shell_tools = [item for item in descriptors if item.server_id == SHELL_SERVER_ID]
        assert RUN_TOOL_NAME in {item.tool_name for item in shell_tools}

        discovered = await gateway.invoke(_invocation(str(inventory.tool_id), {}))
        assert discovered.success is True, discovered.error
        sessions = discovered.output["sessions"]
        assert [entry["session_ref"] for entry in sessions] == ["alice", "bob"]

        executed = await gateway.invoke(
            _invocation(
                "analysis." + RUN_TOOL_NAME,
                {"workroom_key": WORKROOM_KEY, "language": "bash", "script": "echo retained"},
            )
        )
        assert executed.success is True, executed.error
        assert executed.output["stdout"] == "fake:echo retained"
