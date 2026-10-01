"""Pinned workspace profile contracts. No new Core capability/target type."""

from __future__ import annotations

from dataclasses import dataclass

from redteam.capability.servers.workspace import (
    DirectoryOutput,
    FileOutput,
    MutationOutput,
    SearchOutput,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel


@dataclass(frozen=True)
class WorkspaceToolSpec:
    operation: str
    required: frozenset[str]
    optional: frozenset[str]
    output_schema: dict


_CONTEXT = frozenset({"engagement_id", "run_id"})
WORKSPACE_READ_TOOLS = {
    "list_directory": WorkspaceToolSpec(
        "directory_list",
        frozenset({"asset_ref"}),
        _CONTEXT | {"path"},
        DirectoryOutput.model_json_schema(),
    ),
    "read_file": WorkspaceToolSpec(
        "file_read",
        frozenset({"asset_ref", "path"}),
        _CONTEXT | {"offset_chars", "max_chars"},
        FileOutput.model_json_schema(),
    ),
    "search_files": WorkspaceToolSpec(
        "file_search",
        frozenset({"asset_ref", "pattern"}),
        _CONTEXT | {"path", "mode"},
        SearchOutput.model_json_schema(),
    ),
}
WORKSPACE_WRITE_TOOLS = {
    "write_file": WorkspaceToolSpec(
        "file_write",
        frozenset({"asset_ref", "path", "content"}),
        _CONTEXT | {"expected_sha256"},
        MutationOutput.model_json_schema(),
    ),
    "apply_patch": WorkspaceToolSpec(
        "file_edit",
        frozenset({"asset_ref", "path", "old_text", "new_text", "expected_sha256"}),
        _CONTEXT,
        MutationOutput.model_json_schema(),
    ),
}


def workspace_specs(profile: str) -> dict[str, WorkspaceToolSpec]:
    if profile == "workspace_read":
        return WORKSPACE_READ_TOOLS
    if profile == "workspace_write":
        return WORKSPACE_WRITE_TOOLS
    raise ValueError("unknown workspace profile")


def minimum_workspace_risk(profile: str) -> RiskLevel:
    return RiskLevel.HIGH if profile == "workspace_write" else RiskLevel.MEDIUM


def validate_workspace_descriptor(
    descriptor: ToolDescriptor,
    profile: str,
    asset_ref: str,
) -> None:
    specs = workspace_specs(profile)
    spec = specs.get(descriptor.tool_name)
    if spec is None or descriptor.capability_tags != ["analysis"]:
        raise ValueError("workspace discovery contains unexpected tool/capability")
    if (
        set(descriptor.required_arguments) != spec.required
        or set(descriptor.optional_arguments) != spec.optional
        or descriptor.operations != [spec.operation]
        or descriptor.target_types != ["asset"]
        or descriptor.output_schema != spec.output_schema
    ):
        raise ValueError("workspace discovery schema/operation differs from pinned manifest")
    properties = descriptor.input_schema.get("properties", {})
    if properties.get("asset_ref", {}).get("const") != asset_ref:
        raise ValueError("workspace discovery asset_ref differs from operator configuration")
    integer_arguments = {"offset_chars", "max_chars"}
    for name, schema in properties.items():
        if name in {"asset_ref", "mode", "expected_sha256"}:
            continue
        expected_type = "integer" if name in integer_arguments else "string"
        if schema.get("type") != expected_type:
            raise ValueError("workspace discovery argument type differs from pinned manifest")
    if "mode" in properties and properties["mode"].get("enum") != ["name", "content"]:
        raise ValueError("workspace discovery search mode differs from pinned manifest")
    if "expected_sha256" in properties:
        digest_schema = properties["expected_sha256"]
        if descriptor.tool_name == "apply_patch":
            valid_digest_type = digest_schema.get("type") == "string"
        else:
            valid_digest_type = digest_schema.get("anyOf") == [{"type": "string"}, {"type": "null"}]
        if not valid_digest_type:
            raise ValueError("workspace discovery digest type differs from pinned manifest")
