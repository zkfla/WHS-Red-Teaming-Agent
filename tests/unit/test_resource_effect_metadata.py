"""Actual generic MCP adapter propagates declared effects with original output."""

import pytest

from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
from redteam.capability.adapters.mcp.transport import RemoteCallResult, RemoteTool
from redteam.capability.settings import MCPConfigurationError
from redteam.core.contracts.tool import PROVIDER_METADATA_RESOURCE_EFFECTS, ToolDescriptor


class TransportDouble:
    def __init__(self, effects):
        self.effects = effects

    async def list_tools(self):
        return [RemoteTool(name="make", description="Create object", input_schema={}, metadata={
            PROVIDER_METADATA_RESOURCE_EFFECTS: self.effects,
        })]

    async def health_check(self):
        return True

    async def call_tool(self, tool_name, arguments):
        return RemoteCallResult(is_error=False, output={"id": "synthetic", "created": True})


@pytest.mark.parametrize("domain", [None, "provider-owned-backend"])
async def test_provider_metadata_reaches_original_adapter_result(domain):
    effects = [{"effect": "created", "resource_type": "object", "identity_path": ["id"],
                "confirmation_path": ["created"]}]
    if domain is not None:
        effects[0].update(version=2, identity_domain=domain)
    adapter = MCPServerAdapter(server_id="synthetic", tool_namespace="synthetic",
                               capability_tags=("web",), transport=TransportDouble(effects))
    tool, = await adapter.discover_tools()
    result = await adapter.invoke(tool, {})
    assert result.output == {"id": "synthetic", "created": True}
    assert result.metadata["resource_effects"][0]["identity_path"] == ["id"]
    assert result.metadata["resource_effects"][0]["version"] == (2 if domain else 1)
    assert result.metadata["resource_effects"][0].get("identity_domain") == domain


@pytest.mark.parametrize("effect", [{}, {"version": 2, "effect": "created",
                                       "resource_type": "object", "identity_path": ["id"],
                                       "confirmation_path": ["created"]}])
async def test_invalid_resource_declaration_refuses_discovery(effect):
    adapter = MCPServerAdapter(server_id="synthetic", tool_namespace="synthetic",
                               capability_tags=("web",), transport=TransportDouble([effect]))
    with pytest.raises(MCPConfigurationError, match="resource effect"):
        await adapter.discover_tools()


def test_absent_effect_keeps_original_descriptor_fingerprint_shape():
    descriptor = ToolDescriptor(tool_id="synthetic.read", server_id="synthetic", tool_name="read")
    assert "resource_effects" not in descriptor.model_dump(mode="json")
