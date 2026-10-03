"""Built-in executable specialist specifications."""

from redteam.attack_graph.patterns import NodePattern
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.base import (
    ASSET_REF_PLACEHOLDER,
    CRASH_ID_PLACEHOLDER,
    DEVICE_REF_PLACEHOLDER,
    PACKAGE_LOG_FILTERS_PLACEHOLDER,
    PACKAGE_NAME_PLACEHOLDER,
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
    # Must match AgentDefinition("web-api-agent").capabilities
    # (orchestration.agents.definition.builtin_agent_definitions) exactly --
    # AgentRegistry.register/register_definition enforce this at
    # registration time so the two can never silently drift again.
    capabilities=["web", "api"],
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
    description="Launches authorized mobile apps and observes device logs and crashes.",
    capabilities=["mobile"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="launch-mobile-app",
            capability="mobile",
            operation="app_launch",
            tool_id=ToolId("mobile.mobile_launch_app"),
            compatible_tool_ids=[ToolId("mobile.mobile_launch_app")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[
                NodePattern(
                    type=GraphNodeType.ASSET,
                    attrs={"in_scope": True},
                    bind="asset",
                )
            ],
            arguments={
                "device": DEVICE_REF_PLACEHOLDER,
                "packageName": PACKAGE_NAME_PLACEHOLDER,
            },
            reason="Launch the configured authorized application before observing runtime output.",
            observed_attrs={"mobile_app_launched": True},
            attempt_key="mobile.app_launch:launch",
        ),
        PlanStep(
            step_id="observe-device-logs",
            capability="mobile",
            operation="response_analysis",
            tool_id=ToolId("mobile.mobile_get_device_logs"),
            compatible_tool_ids=[ToolId("mobile.mobile_get_device_logs")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[
                NodePattern(
                    type=GraphNodeType.ASSET,
                    attrs={"in_scope": True, "mobile_app_launched": True},
                    bind="asset",
                )
            ],
            arguments={
                "device": DEVICE_REF_PLACEHOLDER,
                "filter": PACKAGE_LOG_FILTERS_PLACEHOLDER,
                "limit": 200,
            },
            reason="Observe bounded package-filtered device logs after application launch.",
            observed_attrs={"mobile_logs_observed": True},
            attempt_key="mobile.response_analysis:get_device_logs",
        ),
        PlanStep(
            step_id="list-mobile-crashes",
            capability="mobile",
            operation="response_analysis",
            tool_id=ToolId("mobile.mobile_list_crashes"),
            compatible_tool_ids=[ToolId("mobile.mobile_list_crashes")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[
                NodePattern(
                    type=GraphNodeType.ASSET,
                    attrs={"in_scope": True, "mobile_app_launched": True},
                    bind="asset",
                )
            ],
            arguments={"device": DEVICE_REF_PLACEHOLDER},
            reason="List device crash reports after application launch.",
            observed_attrs={"mobile_crashes_listed": True},
            attempt_key="mobile.response_analysis:list_crashes",
        ),
        PlanStep(
            step_id="inspect-mobile-crash",
            capability="mobile",
            operation="response_analysis",
            tool_id=ToolId("mobile.mobile_get_crash"),
            compatible_tool_ids=[ToolId("mobile.mobile_get_crash")],
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[
                NodePattern(
                    type=GraphNodeType.ASSET,
                    attrs={
                        "in_scope": True,
                        "mobile_app_launched": True,
                        "mobile_crash_observed": True,
                    },
                    bind="asset",
                )
            ],
            arguments={
                "device": DEVICE_REF_PLACEHOLDER,
                "id": CRASH_ID_PLACEHOLDER,
            },
            reason="Retrieve the configured application's observed crash report.",
            observed_attrs={"mobile_crash_retrieved": True},
            attempt_key="mobile.response_analysis:get_crash",
        ),
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
_APKTOOL_ANALYZED_ASSET = NodePattern(
    type=GraphNodeType.ASSET,
    attrs={
        "in_scope": True,
        "analysis_decoded": True,
        "manifest_analyzed": True,
        "smali_inventoried": True,
        "sensitive_strings_searched": True,
    },
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
        PlanStep(
            step_id="inspect-jadx-manifest",
            capability="analysis",
            operation="static_analysis",
            tool_id=ToolId("analysis.get_android_manifest"),
            compatible_tool_ids=[ToolId("analysis.get_android_manifest")],
            require_healthy_tool=True,
            assessment_kind="observation",
            target_bind="asset",
            preconditions=[_APKTOOL_ANALYZED_ASSET.model_copy(deep=True)],
            arguments={},
            reason="Cross-check the analyzed APK manifest through the optional JADX bridge.",
            observed_attrs={"jadx_manifest_analyzed": True},
            attempt_key="analysis.static_analysis:get_android_manifest",
        ),
    ],
)
