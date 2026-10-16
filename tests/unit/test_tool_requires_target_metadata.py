"""Owner-declared target requirement on ToolDescriptor via provider metadata."""

from __future__ import annotations

import pytest

from redteam.capability.adapters.mcp.streamable_http import _apply_provider_metadata
from redteam.capability.servers.web_search import SearchConfig, SearchResult
from redteam.capability.servers.web_search_mcp import PROPOSED_OPERATION, REMOTE_TOOL_NAME
from redteam.capability.web_search_manifest import validate_web_search_discovery
from redteam.core.contracts.tool import PROVIDER_METADATA_REQUIRES_TARGET, ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId


def _descriptor(**updates):
    values = dict(
        tool_id=ToolId("recon.provider.ping"),
        server_id="provider",
        tool_name="ping",
        description="harmless probe",
        input_schema={"type": "object"},
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    values.update(updates)
    return ToolDescriptor.model_validate(values)


def test_requires_target_defaults_to_true() -> None:
    assert _descriptor().requires_target is True


def test_owner_false_roundtrips_through_provider_metadata() -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_REQUIRES_TARGET: False})
    assert descriptor.requires_target is False


async def test_web_session_inventory_discovery_remains_targetless() -> None:
    from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
    from redteam.capability.adapters.mcp.transport import (
        RemoteCallResult,
        RemoteTool,
        remote_tool_metadata,
    )
    from redteam.capability.servers.config import ServerPolicy, TargetProfile
    from redteam.capability.servers.web_http import create_server
    from redteam.orchestration.knowledge_validation import InternalAssessment
    from redteam.orchestration.native_binding import resolve_native_target

    server = create_server(ServerPolicy(
        targets={"inventory-target": TargetProfile(origin="http://127.0.0.1", allowed_paths=["/"])},
        allowed_networks=["127.0.0.1/32"],
    ))

    class DiscoveryOnlyTransport:
        async def list_tools(self) -> list[RemoteTool]:
            return [RemoteTool(
                name=tool.name, description=tool.description or "",
                input_schema=dict(tool.input_schema),
                metadata=remote_tool_metadata(tool),
            ) for tool in await server.list_tools()]

        async def health_check(self) -> bool:
            return True

        async def call_tool(self, tool_name: str, arguments: dict) -> RemoteCallResult:
            raise AssertionError("discovery must not execute a tool")

    adapter = MCPServerAdapter(
        server_id="web-http", tool_namespace="web", capability_tags=("web",),
        transport=DiscoveryOnlyTransport(),
    )
    descriptors = {tool.tool_name: tool for tool in await adapter.discover_tools()}
    inventory = descriptors["session_list"]
    assert inventory.requires_target is False
    assert all(tool.requires_target for name, tool in descriptors.items() if name != "session_list")
    assert await resolve_native_target(None, inventory, {}, InternalAssessment()) is None


def test_owner_true_roundtrips_through_provider_metadata() -> None:
    descriptor = _descriptor(requires_target=False)
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_REQUIRES_TARGET: True})
    assert descriptor.requires_target is True


def test_absent_key_keeps_default() -> None:
    assert _descriptor().requires_target is True
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, {})
    assert descriptor.requires_target is True
    declared = _descriptor(requires_target=False)
    _apply_provider_metadata(declared, {})
    assert declared.requires_target is False


def test_none_metadata_keeps_default() -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, None)
    assert descriptor.requires_target is True


@pytest.mark.parametrize("bad", ["true", "false", "False", "1", 1, 0, 1.0, 0.0, [], {}, (False,)])
def test_invalid_type_does_not_coerce(bad: object) -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_REQUIRES_TARGET: bad})
    assert descriptor.requires_target is True


@pytest.mark.parametrize("bad", ["false", "true", 1, 0, None, [], {}])
def test_invalid_type_does_not_clear_declared_false(bad: object) -> None:
    descriptor = _descriptor(requires_target=False)
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_REQUIRES_TARGET: bad})
    assert descriptor.requires_target is False


def test_does_not_infer_from_target_binding_or_target_types() -> None:
    descriptor = _descriptor(target_types=[])
    _apply_provider_metadata(
        descriptor, {"redteam.target_binding": {"binds_target_arguments": False}}
    )
    assert descriptor.requires_target is True


def test_descriptor_model_roundtrip_preserves_owner_value() -> None:
    descriptor = _descriptor(requires_target=False)
    restored = ToolDescriptor.model_validate(descriptor.model_dump())
    assert restored.requires_target is False


def _web_search_descriptor(config: SearchConfig, **updates):
    values = dict(
        tool_id=ToolId(f"web.{REMOTE_TOOL_NAME}"),
        server_id="web-search",
        tool_name=REMOTE_TOOL_NAME,
        description="web search",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": config.max_results,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema=SearchResult.model_json_schema(),
        operations=[PROPOSED_OPERATION],
        target_types=[],
        outbound_text_fields=["query"],
        requires_target=False,
    )
    values.update(updates)
    return ToolDescriptor.model_validate(values)


def test_web_search_manifest_requires_targetless() -> None:
    config = SearchConfig(external_search_enabled=True, max_results=5)
    validate_web_search_discovery(_web_search_descriptor(config), config)
    with pytest.raises(ValueError, match="discovery differs"):
        validate_web_search_discovery(_web_search_descriptor(config, requires_target=True), config)
