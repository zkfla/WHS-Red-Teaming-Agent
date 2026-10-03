"""Map bounded apktool results into ASSET facts and manifest findings."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.target import AnyTarget, AssetTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType, Verdict
from redteam.orchestration.expectations import Expectation, verdict_for
from redteam.orchestration.observations.mapper import (
    FindingMutation,
    GraphMutation,
    NodeMutation,
)

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ANDROID_NAME = f"{{{ANDROID_NS}}}name"
ANDROID_EXPORTED = f"{{{ANDROID_NS}}}exported"
ANDROID_DEBUGGABLE = f"{{{ANDROID_NS}}}debuggable"
ANDROID_CLEARTEXT = f"{{{ANDROID_NS}}}usesCleartextTraffic"
MAX_MANIFEST_CHARS = 2_000_000

COMPONENT_TAGS = frozenset({"activity", "activity-alias", "service", "receiver", "provider"})
DANGEROUS_PERMISSIONS = frozenset(
    {
        "android.permission.ACCESS_BACKGROUND_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.CALL_PHONE",
        "android.permission.CAMERA",
        "android.permission.GET_ACCOUNTS",
        "android.permission.READ_CALL_LOG",
        "android.permission.READ_CONTACTS",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.READ_PHONE_STATE",
        "android.permission.READ_SMS",
        "android.permission.RECORD_AUDIO",
        "android.permission.SEND_SMS",
        "android.permission.WRITE_CALL_LOG",
        "android.permission.WRITE_CONTACTS",
        "android.permission.WRITE_EXTERNAL_STORAGE",
    }
)


@dataclass(frozen=True)
class _ManifestCheck:
    check_id: str
    field: str
    title: str
    description: str
    severity: str


_CHECKS = (
    _ManifestCheck(
        "android_manifest_debuggable",
        "debuggable",
        "Android application is debuggable",
        "The production-like APK enables android:debuggable.",
        "high",
    ),
    _ManifestCheck(
        "android_manifest_cleartext",
        "cleartext_traffic_allowed",
        "Android application explicitly allows cleartext traffic",
        "The manifest explicitly sets usesCleartextTraffic=true.",
        "medium",
    ),
    _ManifestCheck(
        "android_manifest_exported_components",
        "has_exported_components",
        "Android application exposes components",
        "One or more Android components are explicitly exported.",
        "medium",
    ),
    _ManifestCheck(
        "android_manifest_dangerous_permissions",
        "has_dangerous_permissions",
        "Android application requests dangerous permissions",
        "The APK requests one or more security-sensitive Android permissions.",
        "medium",
    ),
)


def _stable_id(prefix: str, target: AssetTarget, check_id: str) -> str:
    payload = f"{node_id_for_target(target)}|{check_id}".encode()
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _successful_output(result: ToolResult) -> dict | None:
    if not result.success or not isinstance(result.output, dict):
        return None
    return result.output if result.output.get("success") is True else None


def map_apktool_decode(
    *, target: AnyTarget, arguments: dict, result: ToolResult
) -> GraphMutation:
    del arguments
    output = _successful_output(result)
    if not isinstance(target, AssetTarget) or output is None:
        return GraphMutation()
    project_dir = output.get("output_dir")
    if not isinstance(project_dir, str) or not project_dir.strip():
        return GraphMutation()
    return GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ASSET,
                node_id=node_id_for_target(target),
                attrs={"analysis_decoded": True},
                facts={"project_dir": project_dir.strip()},
            )
        ]
    )


def _manifest_root(result: ToolResult) -> ET.Element | None:
    output = _successful_output(result)
    manifest = output.get("manifest") if output is not None else None
    if (
        not isinstance(manifest, str)
        or not manifest.strip()
        or len(manifest) > MAX_MANIFEST_CHARS
        or "<!DOCTYPE" in manifest.upper()
    ):
        return None
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError:
        return None
    return root if root.tag == "manifest" else None


def _manifest_facts(root: ET.Element) -> dict:
    application = root.find("application")
    debuggable = application is not None and application.get(ANDROID_DEBUGGABLE) == "true"
    cleartext = application is not None and application.get(ANDROID_CLEARTEXT) == "true"
    exported = sorted(
        f"{element.tag}:{element.get(ANDROID_NAME, '(unnamed)')}"
        for element in root.iter()
        if element.tag in COMPONENT_TAGS and element.get(ANDROID_EXPORTED) == "true"
    )
    permissions = sorted(
        {
            name
            for element in root.findall("uses-permission")
            if isinstance((name := element.get(ANDROID_NAME)), str) and name
        }
    )
    dangerous = sorted(set(permissions) & DANGEROUS_PERMISSIONS)
    return {
        "package_name": root.get("package", ""),
        "debuggable": debuggable,
        "cleartext_traffic_allowed": cleartext,
        "exported_components": exported,
        "has_exported_components": bool(exported),
        "requested_permissions": permissions,
        "dangerous_permissions": dangerous,
        "has_dangerous_permissions": bool(dangerous),
    }


def map_apktool_manifest(
    *, target: AnyTarget, arguments: dict, result: ToolResult
) -> GraphMutation:
    del arguments
    if not isinstance(target, AssetTarget):
        return GraphMutation()
    root = _manifest_root(result)
    if root is None:
        return GraphMutation()

    facts = _manifest_facts(root)
    assessed_result = result.model_copy(update={"output": facts})
    assessments: list[dict] = []
    findings: list[FindingMutation] = []
    target_node_id = node_id_for_target(target)
    for check in _CHECKS:
        expectation = Expectation(
            kind="output_field_equals",
            field=check.field,
            value=True,
            description=check.description,
            violation_if_met=True,
        )
        verdict, reason = verdict_for(expectation, assessed_result)
        assessment = {
            "check_id": check.check_id,
            "expectation": expectation.model_dump(mode="json"),
            "verdict": verdict.value,
            "reason": reason,
        }
        assessments.append(assessment)
        if verdict is not Verdict.VIOLATION:
            continue
        findings.append(
            FindingMutation(
                check_id=check.check_id,
                hypothesis_id=_stable_id("hyp_manifest", target, check.check_id),
                finding_id=_stable_id("find_manifest", target, check.check_id),
                title=check.title,
                description=check.description,
                severity=check.severity,
                expectation=expectation,
                verdict=verdict,
                reason=reason,
                target_node_id=target_node_id,
                metadata={
                    "claim_key": _stable_id("claim_manifest", target, check.check_id),
                    "check_id": check.check_id,
                    "analyzer": "apktool-manifest-v1",
                },
            )
        )

    graph_facts = {
        key: value
        for key, value in facts.items()
        if key not in {"has_exported_components", "has_dangerous_permissions"}
    }
    graph_facts["manifest_assessments"] = assessments
    return GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ASSET,
                node_id=target_node_id,
                attrs={"manifest_analyzed": True},
                facts=graph_facts,
            )
        ],
        findings=findings,
    )
