"""Fail-closed profile checks for the operator-configured page reader MCP tool."""

from __future__ import annotations

from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.servers.page_text import PageTextConfig, PageTextResult
from redteam.capability.servers.page_text_mcp import PROPOSED_OPERATION, REMOTE_TOOL_NAME
from redteam.core.contracts.tool import ToolDescriptor


def validate_page_text_discovery(descriptor: ToolDescriptor, config: PageTextConfig) -> None:
    """Check actual discovery against the same policy used by the server."""
    inputs = descriptor.input_schema
    outputs = descriptor.output_schema
    properties = inputs.get("properties", {})
    maximum = properties.get("max_chars", {}).get("anyOf", [])
    integer_options = [option for option in maximum if option.get("type") == "integer"]
    if (
        descriptor.tool_id
        not in {
            f"web.{REMOTE_TOOL_NAME}",
            build_canonical_mcp_tool_id("web", descriptor.server_id, REMOTE_TOOL_NAME),
        }
        or descriptor.tool_name != REMOTE_TOOL_NAME
        or descriptor.operations != [PROPOSED_OPERATION]
        or descriptor.target_types != ["url"]
        or descriptor.outbound_text_fields != ["url"]
        or set(properties) != {"url", "max_chars"}
        or properties["url"].get("type") != "string"
        or inputs.get("required") != ["url"]
        or len(integer_options) != 1
        or integer_options[0].get("minimum") != 1
        or integer_options[0].get("maximum") != config.max_return_chars
        or outputs != PageTextResult.model_json_schema()
    ):
        raise ValueError("page text discovery differs from operator configuration")
