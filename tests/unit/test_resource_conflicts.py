"""Explicit resource discovery → scheduler → shared durable lease ownership."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
from redteam.capability.adapters.mcp.transport import RemoteTool
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.manifest import validate_tool_manifest
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.core.config import OrchestrationConfig
from redteam.core.contracts.tool import (
    PROVIDER_METADATA_RESOURCE_BINDINGS,
    ToolDescriptor,
    ToolResourceBinding,
)
from redteam.core.ids import AgentId, ToolId
from redteam.core.tool_resources import ResourceBindingError
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from redteam.orchestration.resource_conflicts import resource_conflict_keys
from redteam.orchestration.settings import OrchestrationSettings
from tests.integration._engine_llm_harness import FakeCatalog


def descriptor(*bindings, server="bridge", argument="handle") -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(f"web.{server}.inspect"),
        server_id=server,
        tool_name="inspect",
        input_schema={"type": "object", "properties": {argument: {"type": "string"}}},
        capability_tags=["web"],
        requires_target=False,
        resource_bindings=list(bindings),
    )


class Transport:
    def __init__(self, metadata):
        self.metadata = metadata

    async def health_check(self):
        return True

    async def list_tools(self):
        return [RemoteTool("inspect", "", descriptor().input_schema, metadata=self.metadata)]

    async def call_tool(self, *args):
        raise AssertionError("discovery must not execute a tool")


async def test_resource_metadata_survives_discovery_and_schema_validation():
    rows = [{"namespace": "browser.profile", "argument": "handle"}]
    adapter = MCPServerAdapter(
        server_id="bridge",
        tool_namespace="web",
        capability_tags=("web",),
        transport=Transport({PROVIDER_METADATA_RESOURCE_BINDINGS: rows}),
    )
    (tool,) = await adapter.discover_tools()
    validate_tool_manifest(tool)
    assert tool.resource_bindings == [ToolResourceBinding(**rows[0])]
    assert ToolDescriptor.model_validate_json(tool.model_dump_json()).resource_bindings == (
        tool.resource_bindings
    )


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        "not-a-list",
        [{}],
        [{"namespace": "x", "argument": "handle", "key": "both"}],
        [{"namespace": "x", "argument": "handle", "optional": "false"}],
        [{"namespace": "x", "key": "handle", "unknown_option": True}],
    ],
)
async def test_malformed_metadata_fails_registration_instead_of_dropping_lock(rows):
    adapter = MCPServerAdapter(
        server_id="bridge",
        tool_namespace="web",
        capability_tags=("web",),
        transport=Transport({PROVIDER_METADATA_RESOURCE_BINDINGS: rows}),
    )
    with pytest.raises(MCPConfigurationError, match="invalid provider resource metadata"):
        await adapter.discover_tools()


def test_schema_and_runtime_resource_binding_fail_closed_without_echoing_values():
    tool = descriptor(ToolResourceBinding(namespace="browser.profile", argument="missing"))
    with pytest.raises(MCPConfigurationError, match="invalid resource argument"):
        validate_tool_manifest(tool)
    tool = descriptor(ToolResourceBinding(namespace="browser.profile", argument="handle"))
    for args in ({}, {"handle": None}, {"handle": {"secret": "synthetic-token"}}):
        with pytest.raises(ResourceBindingError) as caught:
            resource_conflict_keys(tool, args)
        assert "synthetic-token" not in str(caught.value)
    optional = descriptor(
        ToolResourceBinding(namespace="browser.profile", argument="handle", optional=True)
    )
    assert resource_conflict_keys(optional, {}) == []
    assert resource_conflict_keys(optional, {"handle": None}) == []
    with pytest.raises(ResourceBindingError):
        resource_conflict_keys(optional, {"handle": 0})


def test_resource_keys_cross_providers_and_aliases_without_implicit_ref_or_server_locks():
    first = descriptor(
        ToolResourceBinding(
            namespace="device", argument="handle", value_map={"alias-a": "physical-one"}
        )
    )
    other = descriptor(
        ToolResourceBinding(
            namespace="device", argument="serial", value_map={"alias-b": "physical-one"}
        ),
        server="other-provider",
        argument="serial",
    )
    keys = resource_conflict_keys(first, {"handle": "alias-a"})
    assert keys == resource_conflict_keys(other, {"serial": "alias-b"})
    assert "physical-one" not in repr(keys) and "alias-a" not in repr(keys)
    assert keys != resource_conflict_keys(
        descriptor(ToolResourceBinding(namespace="workspace", key="physical-one")), {}
    )
    assert resource_conflict_keys(descriptor(), {"unknown_ref": "physical-one"}) == []
    with pytest.raises(ResourceBindingError):
        resource_conflict_keys(first, {"handle": "unmapped"})


async def test_operator_configuration_adds_resources_but_cannot_erase_provider_locks(monkeypatch):
    env = {
        "REDTEAM_MCP_BRIDGE_KIND": "web_api",
        "REDTEAM_MCP_BRIDGE_TRANSPORT": "stdio",
        "REDTEAM_MCP_BRIDGE_COMMAND": "not-executed",
        "REDTEAM_MCP_BRIDGE_TOOL_RESOURCES_JSON": json.dumps(
            {
                "inspect": [
                    {"namespace": "browser.profile", "argument": "handle"},
                ]
            }
        ),
    }
    transport = Transport(
        {
            PROVIDER_METADATA_RESOURCE_BINDINGS: [
                {"namespace": "browser.backend", "key": "physical-backend"},
            ]
        }
    )
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_transport", lambda settings: transport
    )
    gateway = await build_mcp_tool_gateway(["bridge"], env=env, health_refresh_enabled=False)
    tool = gateway.get(ToolId("web.bridge.inspect"))
    assert tool is not None and len(resource_conflict_keys(tool, {"handle": "one"})) == 2
    env["REDTEAM_MCP_BRIDGE_TOOL_RESOURCES_JSON"] = '{"not-discovered": []}'
    with pytest.raises(MCPConfigurationError, match="undiscovered"):
        await build_mcp_tool_gateway(["bridge"], env=env, health_refresh_enabled=False)
    env["REDTEAM_MCP_BRIDGE_TOOL_RESOURCES_JSON"] = '{"inspect": [{"namespace": "bad"}]}'
    with pytest.raises(MCPConfigurationError, match="invalid TOOL_RESOURCES_JSON"):
        MCPServerSettings.from_environment("bridge", env=env)


async def test_compiler_rejects_unbound_resources_and_preserves_bound_keys():
    tool = descriptor(ToolResourceBinding(namespace="browser.profile", argument="handle"))
    draft = CandidateDraft(
        agent_id=AgentId("web-worker"),
        target_node_ids=[],
        plan_step=PlanStep(
            step_id="one",
            binding_mode="selected_tool",
            tool_id=tool.tool_id,
            reason="inspect the configured resource",
            execution_origin_version=1,
            model_tool_call_id="call-one",
            arguments={},
        ),
    )
    compiler = CandidateCompiler(FakeCatalog([tool]))
    candidates, errors = compiler.compile_drafts_with_rejections([draft])
    assert not candidates and errors[0].reason == "resource_binding_invalid"
    bound = draft.model_copy(
        update={
            "plan_step": draft.plan_step.model_copy(
                update={"arguments": {"handle": "private-profile"}}
            )
        }
    )
    candidates, errors = compiler.compile_drafts_with_rejections([bound])
    assert not errors



















async def test_real_mcp_discovery_declares_workspace_and_device_physical_aliases(tmp_path):
    from mcp import Client

    from redteam.capability.adapters.mcp.streamable_http import _apply_provider_metadata
    from redteam.capability.adapters.mcp.transport import remote_tool_metadata
    from redteam.capability.servers.mobile import MobileDiagnosticsPolicy
    from redteam.capability.servers.mobile import create_server as mobile_server
    from redteam.capability.servers.workspace import WorkspacePolicy
    from redteam.capability.servers.workspace import create_server as workspace_server

    root = tmp_path / "private-root"
    root.mkdir(mode=0o700)
    policy = WorkspacePolicy(
        root=str(root), asset_ref="one", engagement_id="eng", run_id="run", writable=True
    )
    workspace_keys = []
    for mode in ("read", "write"):
        async with Client(workspace_server(policy, mode)) as client:
            listed = await client.list_tools()
        tools = listed.tools
        assert tools
        for remote in tools:
            tool = descriptor()
            tool.input_schema = remote.input_schema
            _apply_provider_metadata(tool, remote_tool_metadata(remote))
            workspace_keys.append(resource_conflict_keys(tool, {}))
    assert all(key == workspace_keys[0] and key for key in workspace_keys)
    assert str(root) not in repr(workspace_keys)
    mobile_policy = MobileDiagnosticsPolicy(
        devices={
            "alias-a": {"serial": "emulator-5554"},
            "alias-b": {"serial": "emulator-5554"},
            "independent": {"serial": "emulator-5556"},
        }
    )
    async with Client(mobile_server(mobile_policy)) as client:
        listed = await client.list_tools()
    (remote,) = listed.tools
    tool = descriptor()
    tool.input_schema = remote.input_schema
    _apply_provider_metadata(tool, remote_tool_metadata(remote))
    first = resource_conflict_keys(tool, {"device_ref": "alias-a"})
    assert first == resource_conflict_keys(tool, {"device_ref": "alias-b"})
    assert first != resource_conflict_keys(tool, {"device_ref": "independent"})
    assert "emulator-5554" not in repr(remote_tool_metadata(remote))


def test_wait_limit_is_explicit_positive_finite_runtime_configuration():
    config = OrchestrationConfig.from_environment({"REDTEAM_RESOURCE_WAIT_TIMEOUT_SECONDS": "2.5"})
    assert OrchestrationSettings.from_config(config).resource_wait_timeout_seconds == 2.5
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            OrchestrationConfig(resource_wait_timeout_seconds=value)
