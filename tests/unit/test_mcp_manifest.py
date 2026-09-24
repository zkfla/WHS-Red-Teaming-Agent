"""Manifest framework logic and in-process discovery validation.

The in-process tests below run a *real* ``tools/list`` against the local
fallback MCP servers and check the manifest against it -- no external server,
network, or emulator required -- so manifest/discovery drift for our own tools
is caught in ordinary unit runs.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from mcp import Client

from redteam.capability.manifest import (
    CandidateManifest,
    ManifestDiscoveryMismatch,
    ManifestError,
    RiskLevel,
    TargetKind,
    ToolManifestEntry,
    ToolOrigin,
    argument_contract,
)
from redteam.capability.mobile_analysis_manifest import (
    MOBILE_ANALYSIS_MANIFEST,
    SERVER_ANALYSIS_LOCAL,
    SERVER_MOBILE_LOCAL,
)
from redteam.capability.servers import analysis, mobile


def _entry(**overrides: object) -> ToolManifestEntry:
    base: dict[str, object] = dict(
        tool_id="analysis.decode_apk",
        server_id="analysis-apktool",
        tool_name="decode_apk",
        capability="analysis",
        operations=("static_analysis",),
        required_arguments=("apk_path",),
        optional_arguments=("force",),
        target_kind=TargetKind.APK_FILE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    )
    base.update(overrides)
    return ToolManifestEntry(**base)  # type: ignore[arg-type]


def test_argument_contract_splits_required_and_optional() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {}, "b": {}, "c": {}},
        "required": ["a", "b"],
    }
    required, optional = argument_contract(schema)
    assert required == {"a", "b"}
    assert optional == {"c"}


def test_argument_contract_handles_missing_keys() -> None:
    assert argument_contract({}) == (frozenset(), frozenset())
    assert argument_contract({"properties": {"x": {}}}) == (frozenset(), {"x"})


def test_entry_rejects_argument_in_both_required_and_optional() -> None:
    with pytest.raises(ManifestError, match="both required and optional"):
        _entry(required_arguments=("apk_path",), optional_arguments=("apk_path",))


def test_manifest_rejects_duplicate_tool_ids() -> None:
    with pytest.raises(ManifestError, match="duplicate tool_id"):
        CandidateManifest([_entry(), _entry()])


def _snapshot(entry: ToolManifestEntry) -> dict[str, object]:
    return {
        "name": entry.tool_name,
        "input_schema": {
            "type": "object",
            "properties": {name: {} for name in entry.argument_names},
            "required": list(entry.required_arguments),
        },
    }


def test_validate_against_discovery_accepts_matching_snapshot() -> None:
    manifest = CandidateManifest([_entry()])
    manifest.validate_against_discovery("analysis-apktool", [_snapshot(_entry())])


def test_validate_against_discovery_rejects_absent_tool() -> None:
    manifest = CandidateManifest([_entry()])
    with pytest.raises(ManifestDiscoveryMismatch, match="absent from live discovery"):
        manifest.validate_against_discovery("analysis-apktool", [])


def test_validate_against_discovery_rejects_required_drift() -> None:
    manifest = CandidateManifest([_entry()])
    drifted = {
        "name": "decode_apk",
        "input_schema": {
            "type": "object",
            "properties": {"apk_path": {}, "force": {}},
            "required": ["apk_path", "force"],  # server now requires 'force'
        },
    }
    with pytest.raises(ManifestDiscoveryMismatch, match="required arguments drifted"):
        manifest.validate_against_discovery("analysis-apktool", [drifted])


def test_validate_against_discovery_rejects_optional_drift() -> None:
    manifest = CandidateManifest([_entry()])
    drifted = {
        "name": "decode_apk",
        "input_schema": {
            "type": "object",
            "properties": {"apk_path": {}, "force": {}, "extra": {}},
            "required": ["apk_path"],
        },
    }
    with pytest.raises(ManifestDiscoveryMismatch, match="optional arguments drifted"):
        manifest.validate_against_discovery("analysis-apktool", [drifted])


# --- in-process discovery of the local fallback servers ---------------------


async def _list_tools(server: object) -> list[object]:
    async with Client(server) as client:  # type: ignore[arg-type]
        listing = await client.list_tools()
    return list(getattr(listing, "tools", listing))


async def test_local_mobile_manifest_matches_real_discovery() -> None:
    policy = mobile.MobileDiagnosticsPolicy.model_validate(
        {"devices": {"pixel": {"serial": "emulator-5554"}}}
    )
    tools = await _list_tools(mobile.create_server(policy))
    MOBILE_ANALYSIS_MANIFEST.validate_against_discovery(SERVER_MOBILE_LOCAL, tools)


async def test_local_analysis_manifest_matches_real_discovery(tmp_path: Path) -> None:
    apk = tmp_path / "sample.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"placeholder")
    policy = analysis.AnalysisServerPolicy.model_validate(
        {"apks": {"sample": {"path": str(apk)}}}
    )
    tools = await _list_tools(analysis.create_server(policy))
    MOBILE_ANALYSIS_MANIFEST.validate_against_discovery(SERVER_ANALYSIS_LOCAL, tools)
