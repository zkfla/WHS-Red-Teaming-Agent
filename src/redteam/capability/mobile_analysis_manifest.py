"""Capability-owned metadata for the selected mobile and analysis MCP tools.

This is a discovery conformance manifest, not an Agent-facing Core contract.
The Core ToolGateway port currently exposes invocation only; exposing these
records to Agent requires the PM's separate Contract v0.1 change process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redteam.capability.servers.analysis import (
    ApkEntryDigestOutput,
    ApkFileListingOutput,
    ApkStringSearchOutput,
)
from redteam.capability.servers.mobile import DeviceShellOutput
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel


@dataclass(frozen=True, slots=True)
class MobileAnalysisToolSpec:
    tool_id: str
    capability: str
    operation: str
    required_arguments: frozenset[str]
    optional_arguments: frozenset[str]
    target_type: str
    risk_level: RiskLevel
    output_schema: dict[str, Any] | None = None


def _spec(
    capability: str,
    name: str,
    operation: str,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    *,
    risk: RiskLevel = RiskLevel.LOW,
    output_schema: dict[str, Any] | None = None,
) -> MobileAnalysisToolSpec:
    return MobileAnalysisToolSpec(
        tool_id=f"{capability}.{name}",
        capability=capability,
        operation=operation,
        required_arguments=frozenset(required),
        optional_arguments=frozenset(optional),
        target_type="asset",
        risk_level=risk,
        output_schema=output_schema,
    )


# Server IDs match the candidate examples in .env.example. External output
# schemas are None because those MCP servers do not advertise one.
MOBILE_ANALYSIS_TOOLS: dict[str, tuple[MobileAnalysisToolSpec, ...]] = {
    "mobile-next": (
        _spec("mobile", "mobile_launch_app", "app_launch", ("device", "packageName"),
              ("locale",), risk=RiskLevel.MEDIUM),
        _spec("mobile", "mobile_get_device_logs", "response_analysis", ("device",),
              ("filter", "limit", "saveTo"), risk=RiskLevel.MEDIUM),
        _spec("mobile", "mobile_list_crashes", "response_analysis", ("device",),
              risk=RiskLevel.MEDIUM),
        _spec("mobile", "mobile_get_crash", "response_analysis", ("device", "id"),
              risk=RiskLevel.MEDIUM),
    ),
    "mobile-local": (
        _spec("mobile", "device_shell", "device_shell", ("device_ref", "command"),
              output_schema=DeviceShellOutput.model_json_schema()),
    ),
    "apktool": (
        _spec("analysis", "decode_apk", "static_analysis", ("apk_path",),
              ("force", "no_res", "no_src", "output_dir", "timeout")),
        _spec("analysis", "get_manifest", "static_analysis", ("project_dir",)),
        _spec("analysis", "get_apktool_yml", "static_analysis", ("project_dir",)),
        _spec("analysis", "list_smali_directories", "static_analysis", ("project_dir",)),
        _spec("analysis", "list_smali_files", "static_analysis", ("project_dir",),
              ("count", "offset", "package_prefix", "smali_dir")),
        _spec("analysis", "get_smali_file", "static_analysis",
              ("project_dir", "class_name")),
        _spec("analysis", "list_resources", "static_analysis", ("project_dir",),
              ("count", "offset", "resource_type")),
        _spec("analysis", "get_resource_file", "static_analysis",
              ("project_dir", "resource_type", "resource_name")),
        _spec("analysis", "search_in_files", "static_analysis",
              ("project_dir", "search_pattern"),
              ("case_sensitive", "count", "file_extensions", "max_results", "offset")),
    ),
    "analysis-local": (
        _spec("analysis", "apk_file_listing", "static_analysis", ("apk_ref",),
              output_schema=ApkFileListingOutput.model_json_schema()),
        _spec("analysis", "apk_entry_digest", "static_analysis",
              ("apk_ref", "entry_name"),
              output_schema=ApkEntryDigestOutput.model_json_schema()),
        _spec("analysis", "apk_string_search", "static_analysis",
              ("apk_ref", "entry_name", "pattern"),
              output_schema=ApkStringSearchOutput.model_json_schema()),
    ),
}


def validate_discovered_schema(
    spec: MobileAnalysisToolSpec,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None = None,
) -> None:
    """Fail when an MCP server changes required/optional arguments or output fields."""

    properties = input_schema.get("properties")
    required = input_schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError(f"{spec.tool_id}: invalid MCP input schema")
    actual_required = set(required)
    actual_optional = set(properties) - actual_required
    if actual_required != spec.required_arguments or actual_optional != spec.optional_arguments:
        raise ValueError(f"{spec.tool_id}: MCP input schema differs from manifest")

    if spec.output_schema is None:
        return
    if output_schema is None:
        raise ValueError(f"{spec.tool_id}: MCP output schema is missing")
    expected_fields = set(spec.output_schema.get("properties", {}))
    actual_fields = set(output_schema.get("properties", {}))
    expected_required = set(spec.output_schema.get("required", []))
    actual_output_required = set(output_schema.get("required", []))
    if actual_fields != expected_fields or actual_output_required != expected_required:
        raise ValueError(f"{spec.tool_id}: MCP output schema differs from manifest")
    for field in expected_fields:
        expected_shape = _schema_shape(spec.output_schema["properties"][field])
        actual_shape = _schema_shape(output_schema["properties"][field])
        if expected_shape != actual_shape:
            raise ValueError(f"{spec.tool_id}: MCP output schema differs from manifest")
    if _schema_shape(spec.output_schema.get("$defs", {})) != _schema_shape(
        output_schema.get("$defs", {})
    ):
        raise ValueError(f"{spec.tool_id}: MCP output schema differs from manifest")


def validate_configured_discovery(
    server_id: str,
    descriptors: list[ToolDescriptor],
    operation_bindings: dict[str, tuple[str, ...]],
    output_schemas: dict[str, dict[str, Any] | None],
) -> None:
    """Reject drift in configured mobile/analysis candidates at startup.

    The server ID and explicit operation bindings identify a selected candidate.
    Tests that reuse a server ID with a generic MCP fixture and no bindings are
    intentionally outside this candidate check.
    """

    specs = MOBILE_ANALYSIS_TOOLS.get(server_id)
    if specs is None:
        return
    expected = {spec.tool_id: spec for spec in specs}
    actual = {str(descriptor.tool_id): descriptor for descriptor in descriptors}
    if set(actual) != set(expected):
        raise ValueError(f"{server_id}: discovered tool IDs differ from manifest")
    for tool_id, descriptor in actual.items():
        spec = expected[tool_id]
        if (
            spec.capability not in descriptor.capability_tags
            or descriptor.risk_level != spec.risk_level
            or operation_bindings.get(tool_id) != (spec.operation,)
        ):
            raise ValueError(f"{tool_id}: capability, risk, or operation differs from manifest")
        validate_discovered_schema(
            spec,
            descriptor.input_schema,
            output_schemas.get(descriptor.tool_name),
        )


def _schema_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _schema_shape(item)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_schema_shape(item) for item in value]
    return value
