"""Merged dev contracts: one vocabulary, finalized metadata, and safe admission."""

from __future__ import annotations

import pytest

from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core import action_semantics as shared
from redteam.core.contracts import action_naming as compatibility
from redteam.core.contracts.target import UrlTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import AgentId, GraphNodeId
from redteam.orchestration import action_naming as legacy
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from redteam.orchestration.diagnostics import diagnostic_for_exception
from tests.unit.test_mcp_adapter import _invocation
from tests.unit.test_mcp_manifest import _descriptor, _DiscoveredAdapter


def test_naming_compatibility_paths_export_the_same_core_objects() -> None:
    assert compatibility.CANONICAL_OPERATIONS is shared.CANONICAL_OPERATIONS
    assert legacy.CANONICAL_OPERATIONS is shared.CANONICAL_OPERATIONS
    assert compatibility.canonical_action_key is shared.canonical_action_key
    assert legacy.canonical_action_key is shared.canonical_action_key
    assert compatibility.ActionNamingError is shared.ActionNamingError


@pytest.mark.parametrize("override", [False, True])
def test_all_catalog_views_share_finalized_metadata_and_defensive_copies(override: bool) -> None:
    registry = MCPToolRegistry()
    tool = _descriptor()
    tool.operations = []
    tool.target_types = ["url"]
    registry.register(tool)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    gateway = MCPToolGateway(
        registry,
        {},
        {str(tool.tool_id): ("http_request",)},
        output_schemas={str(tool.tool_id): schema} if override else None,
        target_types={str(tool.tool_id): "asset"} if override else None,
    )
    views = [
        gateway.get(tool.tool_id),
        gateway.all()[0],
        gateway.list_by_capability("web")[0],
        gateway.list_tools_for_action("web", "http_request")[0],
    ]
    assert all(view is not None and view == views[0] for view in views)
    finalized = views[0]
    assert finalized is not None and finalized.operations == ["http_request"]
    candidate = gateway.list_candidate_metadata("web", "http_request")[0]
    assert candidate.output_schema == finalized.output_schema
    assert candidate.target_type == finalized.target_types[0]
    assert candidate.required_arguments == finalized.required_arguments
    assert candidate.optional_arguments == finalized.optional_arguments
    finalized.input_schema.clear()
    finalized.operations.clear()
    assert finalized.output_schema is not None
    finalized.output_schema.clear()
    unchanged = gateway.get(tool.tool_id)
    assert unchanged is not None
    assert unchanged.operations == ["http_request"] and unchanged.input_schema
    assert unchanged.output_schema
    assert registry.get(tool.tool_id) == tool
    assert tool.operations == []  # Resolution never mutates raw discovery storage.


async def test_execution_checks_the_same_output_schema_as_the_agent_sees() -> None:
    adapter = ExampleMCPAdapter()
    tool = (await adapter.discover_tools())[0]
    registry = MCPToolRegistry()
    registry.register(tool)
    gateway = MCPToolGateway(
        registry,
        {adapter.server_id: adapter},
        output_schemas={str(tool.tool_id): {"type": "string"}},
    )
    shown = gateway.get(tool.tool_id)
    assert shown is not None and shown.output_schema == {"type": "string"}
    result = await gateway.invoke(_invocation())
    assert not result.success and result.output is None
    assert result.error is not None and result.error.code == "tool_output_schema_mismatch"


def test_supported_operation_without_binding_is_not_an_unsupported_operation() -> None:
    tool = _descriptor()
    registry = MCPToolRegistry()
    registry.register(tool)
    step = PlanStep(
        step_id="missing-binding",
        capability="web",
        operation="page_read",
        tool_id=tool.tool_id,
        target=UrlTarget(url="https://docs.example/guide"),
        reason="contract only; no network execution",
    )
    draft = CandidateDraft(
        agent_id=AgentId("web-agent"),
        plan_step=step,
        target_node_ids=[GraphNodeId("endpoint:https://docs.example/guide")],
    )
    candidates, rejected = CandidateCompiler(
        MCPToolCatalog(registry)
    ).compile_drafts_with_rejections([draft])
    assert not candidates
    assert rejected[0].reason == "no_tool_for_operation" and rejected[0].field == "operation"
    unsupported = draft.model_copy(
        update={"plan_step": step.model_copy(update={"operation": "future_intent"})}
    )
    _, rejected = CandidateCompiler(MCPToolCatalog(registry)).compile_drafts_with_rejections(
        [unsupported]
    )
    assert rejected[0].reason == "no_tool_for_operation"
    assert rejected[0].field == "operation"


@pytest.mark.parametrize(
    ("capability", "operation", "code", "field"),
    [
        ("SECRET_NAMESPACE", "http_request", "capability_unsupported", "capability"),
        ("web", "SECRET_INTENT", "operation_invalid_format", "operation"),
        ("web", "Bearer secret", "operation_invalid_format", "operation"),
    ],
)
def test_naming_diagnostics_keep_reason_and_omit_rejected_values(
    capability: str, operation: str, code: str, field: str
) -> None:
    with pytest.raises(shared.ActionNamingError) as caught:
        shared.canonical_action_key(capability, operation)
    diagnostic = diagnostic_for_exception(caught.value, stage="action_compilation")
    assert diagnostic.code == code and diagnostic.location == f"$.{field}"
    assert "secret" not in diagnostic.model_dump_json()


@pytest.mark.parametrize(
    ("declared", "configured", "expected"),
    [
        ([], None, []),
        (["http_request"], None, ["http_request"]),
        ([], '["http_request"]', ["http_request"]),
        (["http_request"], '["page_read"]', None),
    ],
)
async def test_bootstrap_requires_explicit_or_declared_binding(
    monkeypatch: pytest.MonkeyPatch,
    declared: list[str],
    configured: str | None,
    expected: list[str] | None,
) -> None:
    from redteam.capability.bootstrap import build_mcp_tool_gateway
    from redteam.capability.settings import MCPConfigurationError

    prefix = "REDTEAM_MCP_WEB_MANIFEST_TEST"
    for key, value in {
        "KIND": "web_api",
        "TRANSPORT": "stdio",
        "COMMAND": "unused-fake-command",
    }.items():
        monkeypatch.setenv(f"{prefix}_{key}", value)
    if configured is None:
        monkeypatch.delenv(f"{prefix}_TOOL_OPERATIONS_JSON", raising=False)
    else:
        monkeypatch.setenv(f"{prefix}_TOOL_OPERATIONS_JSON", '{"http_request":' + configured + "}")
    tool: ToolDescriptor = _descriptor(server_id="web-manifest-test", operations=declared)
    adapter = _DiscoveredAdapter(tool.server_id, tool)
    monkeypatch.setattr("redteam.capability.bootstrap._create_transport", lambda settings: object())
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_adapter",
        lambda settings, transport, bundles=None: adapter,
    )
    if expected is None:
        with pytest.raises(MCPConfigurationError, match="conflict"):
            await build_mcp_tool_gateway([tool.server_id])
    else:
        gateway = await build_mcp_tool_gateway([tool.server_id])
        shown = gateway.get(tool.tool_id)
        assert shown is not None and shown.operations == expected
        assert bool(gateway.list_tools_for_action("web", "http_request")) == bool(expected)
