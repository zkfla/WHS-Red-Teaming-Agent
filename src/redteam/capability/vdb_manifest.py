"""Read-only bridge contract, not the full upstream MCP tool inventory."""

from __future__ import annotations

from redteam.capability.servers.vdb_extended import MAX_BATCH_ITEMS, VDBBatch
from redteam.capability.servers.vdb_readonly import CVE_PATTERN, GHSA_PATTERN, VDBLookup, VDBStatus
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

VDB_EXTENDED_TOOLS = {
    **VDB_TOOLS,
    "search_by_ghsa": WorkspaceToolSpec(
        "static_analysis",
        frozenset({"asset_ref", "ghsa_id"}),
        _CONTEXT | {"limit", "sources"},
        VDBLookup.model_json_schema(),
    ),
    "search_by_cpe_like": WorkspaceToolSpec(
        "static_analysis",
        frozenset({"asset_ref", "cpe"}),
        _CONTEXT | {"limit", "sources"},
        VDBLookup.model_json_schema(),
    ),
    "search_packages": WorkspaceToolSpec(
        "static_analysis",
        frozenset({"asset_ref", "purls"}),
        _CONTEXT | {"limit", "sources"},
        VDBBatch.model_json_schema(),
    ),
    "search_sbom": WorkspaceToolSpec(
        "static_analysis",
        frozenset({"asset_ref", "sbom_ref"}),
        _CONTEXT | {"limit", "sources", "offset", "max_items"},
        VDBBatch.model_json_schema(),
    ),
}


def vdb_specs(profile: str) -> dict[str, WorkspaceToolSpec]:
    if profile == "vdb_readonly":
        return VDB_TOOLS
    if profile == "vdb_readonly_extended":
        return VDB_EXTENDED_TOOLS
    raise ValueError("vdb unknown profile")


def validate_vdb_descriptor(
    descriptor: ToolDescriptor,
    asset_ref: str,
    profile: str = "vdb_readonly",
) -> None:
    spec = vdb_specs(profile).get(descriptor.tool_name)
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
        expected_type = (
            "integer"
            if name in {"limit", "offset", "max_items"}
            else "array"
            if name in {"purls", "sources"}
            else "string"
        )
        if schema.get("type") != expected_type:
            raise ValueError("vdb argument type drift")
    if "limit" in properties:
        bound = properties["limit"]
        if bound.get("minimum") != 1 or not 1 <= bound.get("maximum", 0) <= 50:
            raise ValueError("vdb missing bounded result limit")
    if "cve_id" in properties and properties["cve_id"].get("pattern") != CVE_PATTERN:
        raise ValueError("vdb CVE validation drift")
    if "purl" in properties and properties["purl"].get("maxLength") != 512:
        raise ValueError("vdb PURL bound drift")
    if "ghsa_id" in properties and properties["ghsa_id"].get("pattern") != GHSA_PATTERN:
        raise ValueError("vdb GHSA validation drift")
    if "cpe" in properties and properties["cpe"].get("maxLength") != 512:
        raise ValueError("vdb CPE bound drift")
    if "purls" in properties:
        schema = properties["purls"]
        if (
            schema.get("minItems") != 1
            or not 1 <= schema.get("maxItems", 0) <= MAX_BATCH_ITEMS
            or schema.get("items", {}).get("type") != "string"
            or schema["items"].get("maxLength") != 512
        ):
            raise ValueError("vdb package list bound drift")
    if "sources" in properties:
        schema = properties["sources"]
        if (
            schema.get("maxItems") != 5
            or schema.get("items", {}).get("pattern") != r"^[a-z][a-z0-9_-]{0,31}$"
        ):
            raise ValueError("vdb source filter drift")
    if "sbom_ref" in properties and properties["sbom_ref"].get("pattern") != (
        r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    ):
        raise ValueError("vdb SBOM ref drift")
    if "offset" in properties and (
        properties["offset"].get("minimum") != 0 or properties["offset"].get("maximum") != 500
    ):
        raise ValueError("vdb SBOM offset drift")
    if "max_items" in properties and (
        properties["max_items"].get("minimum") != 1
        or not 1 <= properties["max_items"].get("maximum", 0) <= MAX_BATCH_ITEMS
    ):
        raise ValueError("vdb SBOM page size drift")
