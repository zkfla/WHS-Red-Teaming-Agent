"""Structure and content checks for the concrete mobile/analysis manifest."""

from __future__ import annotations

import re

from redteam.capability.adapters.mcp.naming import build_mcp_tool_id
from redteam.capability.manifest import TargetKind, ToolOrigin
from redteam.capability.mobile_analysis_manifest import (
    APKTOOL_MCP_COMMIT,
    MOBILE_ANALYSIS_MANIFEST,
    MOBILE_NEXT_VERSION,
    SERVER_ANALYSIS_APKTOOL,
    SERVER_ANALYSIS_LOCAL,
    SERVER_MOBILE_LOCAL,
    SERVER_MOBILE_NEXT,
)

# Canonical mobile/analysis operations per docs/contracts/actions.md.
_CONTRACT_OPERATIONS = {
    "app_launch",
    "device_shell",
    "runtime_instrumentation",
    "static_analysis",
    "response_analysis",
}
_CANONICAL_OPERATION = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def test_manifest_has_expected_candidate_counts() -> None:
    manifest = MOBILE_ANALYSIS_MANIFEST
    assert len(manifest.entries()) == 17
    assert len(manifest.by_server(SERVER_MOBILE_NEXT)) == 4
    assert len(manifest.by_server(SERVER_ANALYSIS_APKTOOL)) == 9
    assert len(manifest.by_server(SERVER_MOBILE_LOCAL)) == 1
    assert len(manifest.by_server(SERVER_ANALYSIS_LOCAL)) == 3


def test_every_operation_is_canonical_and_in_contract() -> None:
    for entry in MOBILE_ANALYSIS_MANIFEST.entries():
        assert entry.operations, entry.tool_id
        for operation in entry.operations:
            assert _CANONICAL_OPERATION.fullmatch(operation), operation
            assert operation in _CONTRACT_OPERATIONS, operation


def test_tool_ids_match_the_capability_naming_rule() -> None:
    for entry in MOBILE_ANALYSIS_MANIFEST.entries():
        expected = str(build_mcp_tool_id(entry.capability, entry.tool_name))
        assert entry.tool_id == expected
        namespace = entry.tool_id.split(".", 1)[0]
        assert namespace == entry.capability


def test_required_and_optional_arguments_are_disjoint_and_named() -> None:
    for entry in MOBILE_ANALYSIS_MANIFEST.entries():
        assert not (set(entry.required_arguments) & set(entry.optional_arguments))
        assert entry.target_kind is None or isinstance(entry.target_kind, TargetKind)


def test_mobile_next_selection_is_launch_plus_observation() -> None:
    entries = {e.tool_name: e for e in MOBILE_ANALYSIS_MANIFEST.by_server(SERVER_MOBILE_NEXT)}
    assert entries["mobile_launch_app"].operations == ("app_launch",)
    observation = {"mobile_get_device_logs", "mobile_list_crashes", "mobile_get_crash"}
    assert observation <= set(entries)
    for name in observation:
        assert entries[name].operations == ("response_analysis",)


def test_apktool_selection_excludes_write_and_meta_tools() -> None:
    names = {e.tool_name for e in MOBILE_ANALYSIS_MANIFEST.by_server(SERVER_ANALYSIS_APKTOOL)}
    # Write-capable and destructive tools must never be bound.
    for forbidden in ("build_apk", "modify_smali_file", "modify_resource_file", "clean_project"):
        assert forbidden not in names
    assert "decode_apk" in names and "get_manifest" in names
    for entry in MOBILE_ANALYSIS_MANIFEST.by_server(SERVER_ANALYSIS_APKTOOL):
        assert entry.operations == ("static_analysis",)


def test_external_tools_have_no_guessed_output_schema() -> None:
    for entry in MOBILE_ANALYSIS_MANIFEST.entries():
        if entry.origin is ToolOrigin.EXTERNAL:
            assert entry.known_output_schema is None, entry.tool_id


def test_local_fallback_tools_record_their_output_schema() -> None:
    for entry in MOBILE_ANALYSIS_MANIFEST.entries():
        if entry.origin is ToolOrigin.LOCAL_FALLBACK:
            assert entry.known_output_schema is not None, entry.tool_id
            assert entry.known_output_schema.get("type") == "object"


def test_tool_operations_binding_matches_allowlist_for_each_external_server() -> None:
    for server_id in (SERVER_MOBILE_NEXT, SERVER_ANALYSIS_APKTOOL):
        allow = set(MOBILE_ANALYSIS_MANIFEST.allowed_tools_for_server(server_id))
        binding = set(MOBILE_ANALYSIS_MANIFEST.tool_operations_for_server(server_id))
        # bootstrap requires ALLOWED_TOOLS_JSON keys == TOOL_OPERATIONS_JSON keys.
        assert allow == binding
        assert allow


def test_candidate_metadata_exposes_enriched_view() -> None:
    metadata = MOBILE_ANALYSIS_MANIFEST.list_candidate_metadata(capability="analysis")
    assert metadata
    sample = metadata[0]
    for key in ("tool_id", "operations", "target_kind", "risk", "output_schema", "origin"):
        assert key in sample


def test_pins_are_present_for_reproducibility() -> None:
    assert MOBILE_NEXT_VERSION == "1.0.4"
    assert re.fullmatch(r"[0-9a-f]{40}", APKTOOL_MCP_COMMIT)
