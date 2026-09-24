"""Concrete mobile / analysis candidate manifest.

Every entry below was authored from a **real** ``tools/list`` discovery of the
pinned upstreams (not guessed):

* ``mobile-next``    -> ``@mobilenext/mobile-mcp`` (npm, pinned ``1.0.4``), stdio.
                        32 tools discovered; 4 read/observe + launch selected.
* ``analysis-apktool`` -> ``zinja-coder/apktool-mcp-server`` (pinned commit
                        ``12bb33134af905923a0a58643dcc35fd694ad6f6``), stdio.
                        16 tools discovered; 9 read-only static-analysis tools
                        selected (the 4 write tools build/modify/clean and the
                        meta tools health_check/analyze/workspace are excluded).
* ``mobile-local``   -> :mod:`redteam.capability.servers.mobile` (in-repo).
* ``analysis-local`` -> :mod:`redteam.capability.servers.analysis` (in-repo).

External tools expose no ``outputSchema`` in discovery, so their
``known_output_schema`` is ``None`` (we do not guess result structure). Local
fallback tools are ours, so their output schema is recorded and test-checked.
"""

from __future__ import annotations

from redteam.capability.adapters.mcp.naming import build_mcp_tool_id
from redteam.capability.manifest import (
    CandidateManifest,
    TargetKind,
    ToolManifestEntry,
    ToolOrigin,
)
from redteam.core.enums import RiskLevel

MOBILE = "mobile"
ANALYSIS = "analysis"

# Logical server ids used in configuration (REDTEAM_MCP_<ID>_*) and discovery.
SERVER_MOBILE_NEXT = "mobile-next"
SERVER_ANALYSIS_APKTOOL = "analysis-apktool"
SERVER_MOBILE_LOCAL = "mobile-local"
SERVER_ANALYSIS_LOCAL = "analysis-local"

# Pins reproduced by deploy/ and .env.example.
MOBILE_NEXT_VERSION = "1.0.4"
APKTOOL_MCP_COMMIT = "12bb33134af905923a0a58643dcc35fd694ad6f6"


def _entry(
    *,
    server_id: str,
    tool_name: str,
    capability: str,
    operations: tuple[str, ...],
    required: tuple[str, ...],
    optional: tuple[str, ...],
    target_kind: TargetKind | None,
    risk: RiskLevel,
    origin: ToolOrigin,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
) -> ToolManifestEntry:
    return ToolManifestEntry(
        tool_id=str(build_mcp_tool_id(capability, tool_name)),
        server_id=server_id,
        tool_name=tool_name,
        capability=capability,
        operations=operations,
        required_arguments=required,
        optional_arguments=optional,
        target_kind=target_kind,
        risk=risk,
        origin=origin,
        known_input_schema=input_schema,
        known_output_schema=output_schema,
    )


# --- mobile-next (external): 1x app_launch + 3x response_analysis ------------
_MOBILE_NEXT_ENTRIES = [
    _entry(
        server_id=SERVER_MOBILE_NEXT,
        tool_name="mobile_launch_app",
        capability=MOBILE,
        operations=("app_launch",),
        required=("device", "packageName"),
        optional=("locale",),
        target_kind=TargetKind.ANDROID_DEVICE,
        risk=RiskLevel.MEDIUM,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_MOBILE_NEXT,
        tool_name="mobile_get_device_logs",
        capability=MOBILE,
        operations=("response_analysis",),
        required=("device",),
        optional=("limit", "filter", "saveTo"),
        target_kind=TargetKind.ANDROID_DEVICE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_MOBILE_NEXT,
        tool_name="mobile_list_crashes",
        capability=MOBILE,
        operations=("response_analysis",),
        required=("device",),
        optional=(),
        target_kind=TargetKind.ANDROID_DEVICE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_MOBILE_NEXT,
        tool_name="mobile_get_crash",
        capability=MOBILE,
        operations=("response_analysis",),
        required=("device", "id"),
        optional=(),
        target_kind=TargetKind.ANDROID_DEVICE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
]

# --- analysis-apktool (external): 9x static_analysis (read-only) -------------
_APKTOOL_ENTRIES = [
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="decode_apk",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("apk_path",),
        optional=("force", "no_res", "no_src", "output_dir", "timeout"),
        target_kind=TargetKind.APK_FILE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="get_manifest",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir",),
        optional=(),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="get_apktool_yml",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir",),
        optional=(),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="list_smali_directories",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir",),
        optional=(),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="list_smali_files",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir",),
        optional=("smali_dir", "package_prefix", "offset", "count"),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="get_smali_file",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir", "class_name"),
        optional=(),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="list_resources",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir",),
        optional=("resource_type", "offset", "count"),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="get_resource_file",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir", "resource_type", "resource_name"),
        optional=(),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
    _entry(
        server_id=SERVER_ANALYSIS_APKTOOL,
        tool_name="search_in_files",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("project_dir", "search_pattern"),
        optional=("file_extensions", "max_results", "offset", "count", "case_sensitive"),
        target_kind=TargetKind.DECODED_PROJECT,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.EXTERNAL,
    ),
]

# --- mobile-local (local fallback): 1x device_shell -------------------------
_MOBILE_LOCAL_ENTRIES = [
    _entry(
        server_id=SERVER_MOBILE_LOCAL,
        tool_name="device_shell",
        capability=MOBILE,
        operations=("device_shell",),
        required=("device_ref", "command"),
        optional=(),
        target_kind=TargetKind.ANDROID_DEVICE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.LOCAL_FALLBACK,
        output_schema={
            "type": "object",
            "properties": {
                "device_ref": {"type": "string"},
                "command": {"type": "string"},
                "output": {"type": "string"},
                "truncated": {"type": "boolean"},
            },
            "required": ["device_ref", "command", "output", "truncated"],
        },
    ),
]

# --- analysis-local (local fallback): 3x static_analysis --------------------
_ANALYSIS_LOCAL_ENTRIES = [
    _entry(
        server_id=SERVER_ANALYSIS_LOCAL,
        tool_name="apk_file_listing",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("apk_ref",),
        optional=(),
        target_kind=TargetKind.APK_FILE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.LOCAL_FALLBACK,
        output_schema={
            "type": "object",
            "properties": {
                "apk_ref": {"type": "string"},
                "entries": {"type": "array"},
                "limit": {"type": "integer"},
                "may_be_truncated": {"type": "boolean"},
            },
            "required": ["apk_ref", "entries", "limit", "may_be_truncated"],
        },
    ),
    _entry(
        server_id=SERVER_ANALYSIS_LOCAL,
        tool_name="apk_entry_digest",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("apk_ref", "entry_name"),
        optional=(),
        target_kind=TargetKind.APK_FILE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.LOCAL_FALLBACK,
        output_schema={
            "type": "object",
            "properties": {
                "apk_ref": {"type": "string"},
                "entry_name": {"type": "string"},
                "size": {"type": "integer"},
                "sha256": {"type": "string"},
            },
            "required": ["apk_ref", "entry_name", "size", "sha256"],
        },
    ),
    _entry(
        server_id=SERVER_ANALYSIS_LOCAL,
        tool_name="apk_string_search",
        capability=ANALYSIS,
        operations=("static_analysis",),
        required=("apk_ref", "entry_name", "pattern"),
        optional=(),
        target_kind=TargetKind.APK_FILE,
        risk=RiskLevel.LOW,
        origin=ToolOrigin.LOCAL_FALLBACK,
        output_schema={
            "type": "object",
            "properties": {
                "apk_ref": {"type": "string"},
                "entry_name": {"type": "string"},
                "pattern": {"type": "string"},
                "match_count": {"type": "integer"},
                "match_offsets": {"type": "array"},
                "may_be_truncated": {"type": "boolean"},
            },
            "required": [
                "apk_ref",
                "entry_name",
                "pattern",
                "match_count",
                "match_offsets",
                "may_be_truncated",
            ],
        },
    ),
]

MOBILE_ANALYSIS_MANIFEST = CandidateManifest(
    [
        *_MOBILE_NEXT_ENTRIES,
        *_APKTOOL_ENTRIES,
        *_MOBILE_LOCAL_ENTRIES,
        *_ANALYSIS_LOCAL_ENTRIES,
    ]
)
