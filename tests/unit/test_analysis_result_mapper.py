from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.target import AssetTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType, Verdict
from redteam.core.ids import ActionId, AssetId, InvocationId, ToolId
from redteam.orchestration.observations.mapper import (
    GraphMutation,
    default_result_mapper_registry,
)
from redteam.orchestration.observations.mobile_analysis import (
    map_apktool_decode,
    map_apktool_manifest,
)

NOW = datetime.now(timezone.utc)
TARGET = AssetTarget(asset_id=AssetId("insecurebankv2"))


def result(tool_id: str, output: dict | str | None) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_analysis"),
        action_id=ActionId("action_analysis"),
        tool_id=ToolId(tool_id),
        success=True,
        output=output,
        started_at=NOW,
        finished_at=NOW,
    )


MANIFEST_WITH_FINDINGS = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.example.insecure">
  <uses-permission android:name="android.permission.INTERNET" />
  <uses-permission android:name="android.permission.SEND_SMS" />
  <application android:debuggable="true" android:usesCleartextTraffic="true">
    <activity android:name=".ExportedActivity" android:exported="true" />
  </application>
</manifest>
"""


def test_decode_maps_output_dir_to_the_asset_project_dir() -> None:
    mutation = map_apktool_decode(
        target=TARGET,
        arguments={"apk_path": "/work/input/InsecureBankv2.apk"},
        result=result(
            "analysis.decode_apk",
            {"success": True, "output_dir": "/work/output/InsecureBankv2"},
        ),
    )

    assert len(mutation.nodes) == 1
    node = mutation.nodes[0]
    assert node.node_type is GraphNodeType.ASSET
    assert str(node.node_id) == "asset:insecurebankv2"
    assert node.attrs == {"analysis_decoded": True}
    assert node.facts == {"project_dir": "/work/output/InsecureBankv2"}


def test_manifest_maps_four_checks_through_expectation_verdict_and_finding() -> None:
    mutation = map_apktool_manifest(
        target=TARGET,
        arguments={"project_dir": "/work/output/InsecureBankv2"},
        result=result(
            "analysis.get_manifest",
            {"success": True, "manifest": MANIFEST_WITH_FINDINGS},
        ),
    )

    assert len(mutation.nodes) == 1
    facts = mutation.nodes[0].facts
    assert facts["package_name"] == "com.example.insecure"
    assert facts["exported_components"] == ["activity:.ExportedActivity"]
    assert facts["dangerous_permissions"] == ["android.permission.SEND_SMS"]
    assert {item["verdict"] for item in facts["manifest_assessments"]} == {
        Verdict.VIOLATION.value
    }

    assert {finding.check_id for finding in mutation.findings} == {
        "android_manifest_debuggable",
        "android_manifest_cleartext",
        "android_manifest_exported_components",
        "android_manifest_dangerous_permissions",
    }
    assert all(finding.verdict is Verdict.VIOLATION for finding in mutation.findings)
    assert all(finding.expectation.violation_if_met for finding in mutation.findings)


def test_clean_manifest_records_passes_without_inventing_findings() -> None:
    clean = MANIFEST_WITH_FINDINGS.replace(
        '<uses-permission android:name="android.permission.SEND_SMS" />', ""
    ).replace('android:debuggable="true"', 'android:debuggable="false"').replace(
        'android:usesCleartextTraffic="true"',
        'android:usesCleartextTraffic="false"',
    ).replace('android:exported="true"', 'android:exported="false"')

    mutation = map_apktool_manifest(
        target=TARGET,
        arguments={},
        result=result("analysis.get_manifest", {"success": True, "manifest": clean}),
    )

    assert mutation.findings == []
    assert {item["verdict"] for item in mutation.nodes[0].facts["manifest_assessments"]} == {
        Verdict.PASS.value
    }


def test_manifest_mapper_fails_closed_for_malformed_or_unexpected_output() -> None:
    assert map_apktool_manifest(
        target=TARGET,
        arguments={},
        result=result("analysis.get_manifest", {"manifest": "not xml"}),
    ) == GraphMutation()
    assert map_apktool_manifest(
        target=TARGET,
        arguments={},
        result=result("analysis.get_manifest", {"content": MANIFEST_WITH_FINDINGS}),
    ) == GraphMutation()


def test_default_registry_contains_the_apktool_analysis_mappers() -> None:
    registry = default_result_mapper_registry()

    decoded = registry.map_result(
        tool_id=ToolId("analysis.decode_apk"),
        target=TARGET,
        arguments={},
        result=result("analysis.decode_apk", {"success": True, "output_dir": "/work/out"}),
    )
    manifest = registry.map_result(
        tool_id=ToolId("analysis.get_manifest"),
        target=TARGET,
        arguments={},
        result=result(
            "analysis.get_manifest",
            {"success": True, "manifest": MANIFEST_WITH_FINDINGS},
        ),
    )

    assert decoded.nodes
    assert manifest.findings
