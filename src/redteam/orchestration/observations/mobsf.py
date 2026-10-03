"""Map MobSF static observations; only an explicit debuggable rule confirms a finding."""

from __future__ import annotations

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.target import AssetTarget
from redteam.core.enums import GraphNodeType, Verdict
from redteam.orchestration.expectations import Expectation, verdict_for
from redteam.orchestration.observations.mapper import FindingMutation, GraphMutation, NodeMutation
from redteam.orchestration.observations.mobile_analysis import _stable_id


def map_mobsf_report(*, target, arguments, result):
    del arguments
    output = result.output
    if (
        not isinstance(target, AssetTarget)
        or not result.success
        or not isinstance(output, dict)
        or output.get("success") is not True
        or output.get("status") != "completed"
        or not isinstance(output.get("sections"), dict)
    ):
        return GraphMutation()
    sections = output["sections"]
    manifest = sections.get("manifest", {})
    findings = manifest.get("findings", []) if isinstance(manifest, dict) else []
    debuggable = any(
        isinstance(item, dict)
        and item.get("rule") == "app_is_debuggable"
        and item.get("severity") == "high"
        for item in findings
    )
    node_id = node_id_for_target(target)
    mutation = GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ASSET,
                node_id=node_id,
                facts={
                    "mobsf_static_summary": {k: v for k, v in sections.items() if k != "secret"},
                    "mobsf_scan_ref": output.get("scan_ref"),
                    "mobsf_apk_sha256": output.get("sha256"),
                    "mobsf_image": output.get("image"),
                },
            )
        ]
    )
    if not debuggable:
        return mutation
    expectation = Expectation(
        kind="output_field_equals",
        field="debuggable",
        value=True,
        violation_if_met=True,
        description="The approved APK explicitly enables Android debugging.",
    )
    verdict, reason = verdict_for(
        expectation, result.model_copy(update={"output": {"debuggable": True}})
    )
    if verdict is Verdict.VIOLATION:
        check_id = "android_manifest_debuggable"
        mutation.findings.append(
            FindingMutation(
                check_id="android_manifest_debuggable",
                hypothesis_id=_stable_id("hyp_manifest", target, check_id),
                finding_id=_stable_id("find_manifest", target, check_id),
                title="Android application is debuggable",
                description=expectation.description,
                severity="high",
                expectation=expectation,
                verdict=verdict,
                reason=reason,
                target_node_id=node_id,
                metadata={
                    "check_id": "android_manifest_debuggable",
                    "analyzer": "mobsf-static-v1",
                    "claim_key": _stable_id("claim_manifest", target, check_id),
                    "rule_id": "app_is_debuggable",
                },
            )
        )
    return mutation
