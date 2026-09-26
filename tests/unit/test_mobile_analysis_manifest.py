"""Compare owned mobile/analysis metadata with real MCP discovery schemas."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from mcp import Client

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.mobile_analysis_manifest import (
    MOBILE_ANALYSIS_TOOLS,
    validate_configured_discovery,
    validate_discovered_schema,
)
from redteam.capability.servers.analysis import AnalysisServerPolicy
from redteam.capability.servers.analysis import create_server as create_analysis_server
from redteam.capability.servers.mobile import MobileDiagnosticsPolicy
from redteam.capability.servers.mobile import create_server as create_mobile_server
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel
from redteam.core.ids import ToolId


def test_example_bindings_match_manifest() -> None:
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    settings = {}
    for line in example.splitlines():
        text = line.removeprefix("# ")
        if text.startswith("REDTEAM_MCP_") and "=" in text:
            name, value = text.split("=", 1)
            settings[name] = value

    for server_id, specs in MOBILE_ANALYSIS_TOOLS.items():
        prefix = f"REDTEAM_MCP_{server_id.upper().replace('-', '_')}"
        bindings = json.loads(settings[f"{prefix}_TOOL_OPERATIONS_JSON"])
        capability = settings[f"{prefix}_KIND"]
        risk = RiskLevel(settings.get(f"{prefix}_RISK_LEVEL", RiskLevel.LOW.value))
        assert all(spec.capability == capability for spec in specs)
        assert all(spec.tool_id.startswith(f"{capability}.") for spec in specs)
        assert all(spec.risk_level == risk for spec in specs)
        assert bindings == {
            spec.tool_id.partition(".")[2]: [spec.operation] for spec in specs
        }
        if f"{prefix}_ALLOWED_TOOLS_JSON" in settings:
            assert set(json.loads(settings[f"{prefix}_ALLOWED_TOOLS_JSON"])) == set(bindings)


@pytest.mark.parametrize("server_id", ["mobile-local", "analysis-local"])
async def test_local_manifest_matches_mcp_discovery(server_id: str, tmp_path: Path) -> None:
    if server_id == "mobile-local":
        mobile_policy = MobileDiagnosticsPolicy.model_validate(
            {"devices": {"test": {"serial": "emulator-5554"}}}
        )
        server = create_mobile_server(mobile_policy)
    else:
        apk = tmp_path / "test.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"test")
        analysis_policy = AnalysisServerPolicy.model_validate(
            {"apks": {"test": {"path": str(apk)}}}
        )
        server = create_analysis_server(analysis_policy)

    async with Client(server) as client:
        discovered = (await client.list_tools()).tools
    specs = {spec.tool_id.partition(".")[2]: spec for spec in MOBILE_ANALYSIS_TOOLS[server_id]}
    assert {tool.name for tool in discovered} == set(specs)
    for tool in discovered:
        spec = specs[tool.name]
        assert spec.capability in {"mobile", "analysis"}
        assert spec.target_type == "asset"
        validate_discovered_schema(spec, tool.input_schema, tool.output_schema)


@pytest.mark.parametrize("server_id", ["mobile-next", "apktool"])
async def test_external_manifest_matches_live_mcp_discovery(server_id: str) -> None:
    """Opt-in check against an installed external candidate, without a device/APK call."""

    prefix = f"REDTEAM_TEST_{server_id.upper().replace('-', '_')}"
    command = os.environ.get(f"{prefix}_COMMAND")
    if not command:
        pytest.skip(f"set {prefix}_COMMAND and {prefix}_ARGS_JSON for live discovery")
    args = json.loads(os.environ.get(f"{prefix}_ARGS_JSON", "[]"))
    transport = StdioMCPTransport(command, args=args, env=dict(os.environ))
    discovered = {tool.name: tool for tool in await transport.list_tools()}
    for spec in MOBILE_ANALYSIS_TOOLS[server_id]:
        name = spec.tool_id.partition(".")[2]
        assert name in discovered
        validate_discovered_schema(spec, discovered[name].input_schema)


def test_manifest_rejects_required_argument_drift() -> None:
    spec = MOBILE_ANALYSIS_TOOLS["mobile-next"][0]
    with pytest.raises(ValueError, match="input schema differs"):
        validate_discovered_schema(
            spec,
            {"properties": {"device": {}, "packageName": {}}, "required": ["device"]},
        )


def test_manifest_rejects_missing_output_field() -> None:
    spec = MOBILE_ANALYSIS_TOOLS["mobile-local"][0]
    with pytest.raises(ValueError, match="output schema differs"):
        validate_discovered_schema(
            spec,
            {"properties": {"device_ref": {}, "command": {}},
             "required": ["device_ref", "command"]},
            {"properties": {"device_ref": {}, "command": {}, "output": {}},
             "required": ["device_ref", "command", "output"]},
        )


def test_configured_candidate_rejects_runtime_schema_drift() -> None:
    spec = MOBILE_ANALYSIS_TOOLS["mobile-local"][0]
    descriptor = ToolDescriptor(
        tool_id=ToolId(spec.tool_id),
        server_id="mobile-local",
        tool_name="device_shell",
        capability_tags=["mobile"],
        risk_level=spec.risk_level,
        input_schema={
            "type": "object",
            "properties": {"device_ref": {}, "command": {}},
            "required": ["device_ref", "command"],
        },
    )
    bindings: dict[str, tuple[str, ...]] = {spec.tool_id: (spec.operation,)}
    schemas = {descriptor.tool_name: spec.output_schema}
    validate_configured_discovery("mobile-local", [descriptor], bindings, schemas)
    descriptor.input_schema["required"] = ["device_ref"]
    with pytest.raises(ValueError, match="input schema differs"):
        validate_configured_discovery("mobile-local", [descriptor], bindings, schemas)


def test_configured_candidate_rejects_operation_drift() -> None:
    spec = MOBILE_ANALYSIS_TOOLS["mobile-local"][0]
    descriptor = ToolDescriptor(
        tool_id=ToolId(spec.tool_id),
        server_id="mobile-local",
        tool_name="device_shell",
        capability_tags=["mobile"],
        risk_level=spec.risk_level,
        input_schema={
            "type": "object",
            "properties": {"device_ref": {}, "command": {}},
            "required": ["device_ref", "command"],
        },
    )
    with pytest.raises(ValueError, match="operation differs"):
        validate_configured_discovery(
            "mobile-local",
            [descriptor],
            {spec.tool_id: ("response_analysis",)},
            {descriptor.tool_name: spec.output_schema},
        )
