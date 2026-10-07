"""Fail-closed discovery checks for the opt-in Tavily web search profile."""

from __future__ import annotations

from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.servers.web_search import SearchConfig, SearchResult
from redteam.capability.servers.web_search_mcp import PROPOSED_OPERATION, REMOTE_TOOL_NAME
from redteam.core.contracts.tool import ToolDescriptor


def validate_web_search_discovery(descriptor: ToolDescriptor, config: SearchConfig) -> None:
    inputs = descriptor.input_schema
    properties = inputs.get("properties", {})
    query = properties.get("query", {})
    count = properties.get("max_results", {})
    if (
        descriptor.tool_id
        not in {
            f"web.{REMOTE_TOOL_NAME}",
            build_canonical_mcp_tool_id("web", descriptor.server_id, REMOTE_TOOL_NAME),
        }
        or descriptor.tool_name != REMOTE_TOOL_NAME
        or descriptor.operations != [PROPOSED_OPERATION]
        or descriptor.target_types != []
        or descriptor.requires_target is not False
        or descriptor.outbound_text_fields != ["query"]
        or set(properties) != {"query", "max_results"}
        or inputs.get("required") != ["query"]
        or inputs.get("additionalProperties") is not False
        or query.get("type") != "string"
        or query.get("minLength") != 1
        or query.get("maxLength") != 500
        or count.get("type") != "integer"
        or count.get("minimum") != 1
        or count.get("maximum") != config.max_results
        or descriptor.output_schema != SearchResult.model_json_schema()
    ):
        raise ValueError("web search discovery differs from operator configuration")
