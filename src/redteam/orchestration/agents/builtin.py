"""Built-in executable specialist specifications."""

from redteam.attack_graph.patterns import NodePattern
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.base import (
    ASSET_REF_PLACEHOLDER,
    PATH_PLACEHOLDER,
    PROJECT_DIR_PLACEHOLDER,
    TARGET_REF_PLACEHOLDER,
    AgentSpec,
    HypothesisDraft,
    PlanStep,
)

#: Target refs resolve from operator metadata; paths come from graph context.
#: A ServiceTarget without a concrete path does not produce an HTTP request.
_WEB_HTTP_REQUEST_ARGUMENTS = {"target_ref": TARGET_REF_PLACEHOLDER, "path": PATH_PLACEHOLDER}

WEB_API_AGENT = AgentSpec(
    agent_id=AgentId("web-api-agent"),
    name="Web/API Agent",
    description="Inspects discovered HTTP services and endpoints with catalog-bound tools.",
    capabilities=["web"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="inspect-http-endpoint",
            capability="web",
            operation="http_request",
            tool_id=ToolId("web.http_request"),
            compatible_tool_ids=[ToolId("web.http_request")],
            assessment_kind="observation",
            target_bind="endpoint",
            preconditions=[NodePattern(type=GraphNodeType.ENDPOINT, bind="endpoint")],
            arguments=dict(_WEB_HTTP_REQUEST_ARGUMENTS),
            reason="Inspect a discovered endpoint before proposing deeper tests.",
            hypothesis=HypothesisDraft(
                description=(
                    "This endpoint's access-control behavior may require targeted "
                    "validation against an explicit operator policy."
                ),
                confidence=0.2,
                metadata={"kind": "web_access_validation"},
            ),
            observed_attrs={"web_inspected": True},
        ),
        PlanStep(
            step_id="inspect-http-service",
            capability="web",
            operation="http_request",
            tool_id=ToolId("web.http_request"),
            compatible_tool_ids=[ToolId("web.http_request")],
            assessment_kind="observation",
            target_bind="service",
            preconditions=[NodePattern(type=GraphNodeType.SERVICE, bind="service")],
            # `$path` never resolves against a ServiceTarget (see module
            # note) -- this entry stays inert until a real tool exists
            # that can act on a service without a specific path.
            arguments=dict(_WEB_HTTP_REQUEST_ARGUMENTS),
            reason="Inspect a discovered HTTP service and enumerate its visible surface.",
            hypothesis=HypothesisDraft(
                description="The service exposes an HTTP surface suitable for analysis.",
                confidence=0.3,
            ),
            observed_attrs={"web_inspected": True},
        ),
    ],
)


MOBILE_AGENT = AgentSpec(
    agent_id=AgentId("mobile-agent"),
    name="Mobile Agent",
    description="Analyzes discovered APK/IPA artifacts through Capability tools.",
    capabilities=["mobile", "analysis"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="analyze-mobile-artifact",
            capability="analysis",
            operation="static_analysis",
            tool_id=ToolId("analysis.decode_apk"),
            compatible_tool_ids=[ToolId("analysis.decode_apk")],
            target_bind="asset",
            preconditions=[NodePattern(type=GraphNodeType.ASSET, bind="asset")],
            arguments={"apk_path": ASSET_REF_PLACEHOLDER},
            reason="Extract security-relevant structure from the discovered mobile artifact.",
            hypothesis=HypothesisDraft(
                description="The artifact contains security-relevant components or endpoints.",
                confidence=0.3,
            ),
            observed_attrs={"analysis_decoded": True},
            attempt_key="analysis.static_analysis:decode_apk",
        )
    ],
)


_IN_SCOPE_ASSET = NodePattern(
    type=GraphNodeType.ASSET,
    attrs={"in_scope": True},
    bind="asset",
)
_DECODED_ASSET = NodePattern(
    type=GraphNodeType.ASSET,
    attrs={"in_scope": True, "analysis_decoded": True},
    bind="asset",
)


ANALYSIS_AGENT = AgentSpec(
    agent_id=AgentId("analysis-agent"),
    name="Analysis Agent",
    description="Decodes authorized APKs and inspects their manifest and smali surface.",
    capabilities=["analysis"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="decode-apk",
            capability="analysis",
            operation="static_analysis",
            tool_id=ToolId("analysis.decode_apk"),
            compatible_tool_ids=[ToolId("analysis.decode_apk")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[_IN_SCOPE_ASSET.model_copy(deep=True)],
            arguments={"apk_path": ASSET_REF_PLACEHOLDER},
            reason="Decode the authorized APK before inspecting its extracted files.",
            observed_attrs={"analysis_decoded": True},
            attempt_key="analysis.static_analysis:decode_apk",
        ),
        PlanStep(
            step_id="inspect-manifest",
            capability="analysis",
            operation="static_analysis",
            tool_id=ToolId("analysis.get_manifest"),
            compatible_tool_ids=[ToolId("analysis.get_manifest")],
            target_bind="asset",
            preconditions=[_DECODED_ASSET.model_copy(deep=True)],
            arguments={"project_dir": PROJECT_DIR_PLACEHOLDER},
            reason="Inspect AndroidManifest.xml for security-relevant application settings.",
            hypothesis=HypothesisDraft(
                description="The manifest contains an unsafe Android security configuration.",
                confidence=0.4,
                violation_if_confirmed=True,
            ),
            observed_attrs={"manifest_analyzed": True},
            attempt_key="analysis.static_analysis:get_manifest",
        ),
        PlanStep(
            step_id="list-smali",
            capability="analysis",
            operation="static_analysis",
            tool_id=ToolId("analysis.list_smali_files"),
            compatible_tool_ids=[ToolId("analysis.list_smali_files")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[_DECODED_ASSET.model_copy(deep=True)],
            arguments={"project_dir": PROJECT_DIR_PLACEHOLDER},
            reason="Inventory decoded smali files for bounded follow-up analysis.",
            observed_attrs={"smali_inventoried": True},
            attempt_key="analysis.static_analysis:list_smali_files",
        ),
        PlanStep(
            step_id="search-sensitive-strings",
            capability="analysis",
            operation="static_analysis",
            tool_id=ToolId("analysis.search_in_files"),
            compatible_tool_ids=[ToolId("analysis.search_in_files")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[_DECODED_ASSET.model_copy(deep=True)],
            arguments={
                "project_dir": PROJECT_DIR_PLACEHOLDER,
                "search_pattern": "password",
            },
            reason="Search decoded files for password-related hard-coded strings.",
            observed_attrs={"sensitive_strings_searched": True},
            attempt_key="analysis.static_analysis:search_in_files",
        ),
    ],
)
