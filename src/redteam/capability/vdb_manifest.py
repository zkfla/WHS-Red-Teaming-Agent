"""Read-only bridge contract, not the full upstream MCP tool inventory."""

from __future__ import annotations

from redteam.capability.servers.vdb_readonly import CVE_PATTERN, VDBLookup, VDBStatus
from redteam.capability.workspace_manifest import WorkspaceToolSpec
from redteam.core.contracts.tool import ToolDescriptor

_CONTEXT = frozenset({"engagement_id", "run_id"})
VDB_TOOLS = {
    "vdb_status": WorkspaceToolSpec(
        "response_analysis",
        frozenset({"asset_ref"}),
        _CONTEXT,
        VDBStatus.model_json_schema(),
    ),
    "search_by_cve": WorkspaceToolSpec(
        "static_analysis",
        frozenset({"asset_ref", "cve_id"}),
        _CONTEXT | {"limit"},
        VDBLookup.model_json_schema(),
    ),
    "search_by_purl_like": WorkspaceToolSpec(
        "static_analysis",
        frozenset({"asset_ref", "purl"}),
        _CONTEXT | {"limit"},
        VDBLookup.model_json_schema(),
    ),
}


def validate_vdb_descriptor(descriptor: ToolDescriptor, asset_ref: str) -> None:
    spec = VDB_TOOLS.get(descriptor.tool_name)
    if spec is None or descriptor.capability_tags != ["analysis"]:
        raise ValueError("vdb unexpected tool/capability")
    if (
        set(descriptor.required_arguments) != spec.required
        or set(descriptor.optional_arguments) != spec.optional
        or descriptor.operations != [spec.operation]
        or descriptor.target_types != ["asset"]
        or descriptor.output_schema != spec.output_schema
    ):
        raise ValueError("vdb discovery differs from pinned bridge manifest")
    properties = descriptor.input_schema.get("properties", {})
    if set(properties) != spec.required | spec.optional:
        raise ValueError("vdb unexpected input schema properties")
    if properties.get("asset_ref", {}).get("const") != asset_ref:
        raise ValueError("vdb asset differs from operator configuration")
    for name, schema in properties.items():
        if schema.get("type") != ("integer" if name == "limit" else "string"):
            raise ValueError("vdb argument type drift")
    if "limit" in properties:
        bound = properties["limit"]
        if bound.get("minimum") != 1 or not 1 <= bound.get("maximum", 0) <= 50:
            raise ValueError("vdb missing bounded result limit")
    if "cve_id" in properties and properties["cve_id"].get("pattern") != CVE_PATTERN:
        raise ValueError("vdb CVE validation drift")
    if "purl" in properties and properties["purl"].get("maxLength") != 512:
        raise ValueError("vdb PURL bound drift")
