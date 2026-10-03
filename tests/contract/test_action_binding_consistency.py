"""Registration and planning agree on intent, without deriving it from ToolId."""

import pytest

from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.manifest import validate_tool_manifest
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.core.action_semantics import (
    CANONICAL_OPERATIONS,
    canonical_action_key,
)
from redteam.core.contracts.target import UrlTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import AgentId, GraphNodeId, ToolId
from redteam.orchestration import action_naming as legacy
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.agents.llm_provider import LLMCandidateProvider
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft


def descriptor(operation):
    return ToolDescriptor(
        tool_id=ToolId("web.some_remote_tool"),
        server_id="some-server",
        tool_name="some_remote_tool",
        capability_tags=["web"],
        operations=[operation],
        target_types=["url"],
        input_schema={"type": "object"},
    )


@pytest.mark.parametrize("operation", sorted(CANONICAL_OPERATIONS))
def test_every_supported_intent_registers_and_compiles(operation):
    tool = descriptor(operation)
    validate_tool_manifest(tool)
    registry = MCPToolRegistry()
    registry.register(tool)
    catalog = MCPToolCatalog(registry)
    assert legacy.canonical_action_key is canonical_action_key
    assert legacy.CANONICAL_OPERATIONS is CANONICAL_OPERATIONS
    step = PlanStep(
        step_id="shared-contract",
        capability="web",
        operation=operation,
        tool_id=tool.tool_id,
        target=UrlTarget(url="https://docs.example/guide"),
        reason="contract validation, no tool call",
    )
    draft = CandidateDraft(
        agent_id=AgentId("web-agent"),
        plan_step=step,
        target_node_ids=[GraphNodeId("endpoint:https://docs.example/guide")],
    )
    candidates, rejected = CandidateCompiler(catalog).compile_drafts_with_rejections([draft])
    assert len(candidates) == 1 and not rejected


@pytest.mark.parametrize("operation", ["future_intent", "some_remote_tool", "nmap_scan"])
def test_unsupported_lower_snake_case_fails_before_registration(operation, monkeypatch):
    tool = descriptor(operation)
    with pytest.raises(MCPConfigurationError, match="operation_unsupported"):
        validate_tool_manifest(tool)
    registry = MCPToolRegistry()
    registry.register(tool)  # Raw discovery storage is not an executable catalog.
    with pytest.raises(ValueError, match="operation_unsupported"):
        MCPToolCatalog(registry)
    with pytest.raises(ValueError, match="operation_unsupported"):
        MCPToolCatalog(registry, {str(tool.tool_id): [operation]})
    prefix = "REDTEAM_MCP_SOME_SERVER"
    monkeypatch.setenv(f"{prefix}_KIND", "web_api")
    monkeypatch.setenv(f"{prefix}_ENDPOINT", "http://127.0.0.1:12345/mcp")
    monkeypatch.setenv(
        f"{prefix}_TOOL_OPERATIONS_JSON", '{"some_remote_tool":["' + operation + '"]}'
    )
    with pytest.raises(
        MCPConfigurationError, match="some-server.*some_remote_tool.*operation_unsupported"
    ):
        MCPServerSettings.from_environment("some-server")


def test_remote_tool_name_never_becomes_an_intent_implicitly():
    registry = MCPToolRegistry()
    tool = descriptor("http_request")
    tool.operations = []
    tool.tool_name = "http_request"  # Even an identical spelling is not a binding.
    registry.register(tool)
    catalog = MCPToolCatalog(registry)
    assert catalog.list_candidate_metadata() == []
    assert catalog.list_tools_for_action("web", "http_request") == []
    assert not catalog.supports(tool.tool_id, "http_request")


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        ("future_intent", "operation_unsupported"),
        ("HTTP Request", "operation_invalid_format"),
    ],
)
def test_agent_rejects_operation_separately_from_unknown_target(operation, reason):
    provider = LLMCandidateProvider(None)
    action = {"capability": "web", "operation": operation, "target_node_id": "host:docs.example"}
    draft, drop = provider._to_draft(AgentId("web-agent"), action, 0, {"host:docs.example"}, {})
    assert draft is None and drop.reason == reason and drop.field == "operation"
    _, target_drop = provider._to_draft(AgentId("web-agent"), action, 0, set(), {})
    assert target_drop.reason == "unknown_target_node"
    step = PlanStep(
        step_id="valid",
        capability="web",
        operation="http_request",
        tool_id=ToolId("web.some_remote_tool"),
        target=UrlTarget(url="https://docs.example/guide"),
        reason="test",
    )
    valid = CandidateDraft(
        agent_id=AgentId("web-agent"),
        plan_step=step,
        target_node_ids=[GraphNodeId("endpoint:https://docs.example/guide")],
    )
    copied = valid.model_copy(
        update={"plan_step": step.model_copy(update={"operation": operation})}
    )
    candidates, rejected = CandidateCompiler().compile_drafts_with_rejections([copied])
    assert not candidates and rejected[0].reason == reason
    assert rejected[0].field == "operation"
