"""Built-in executable specialist specifications."""

from redteam.attack_graph.patterns import NodePattern
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.base import AgentSpec, HypothesisDraft, PlanStep

WEB_API_AGENT = AgentSpec(
    agent_id=AgentId("web-api-agent"),
    name="Web/API Agent",
    description="Inspects discovered HTTP services and endpoints with catalog-bound tools.",
    capabilities=["web.http.inspect"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="inspect-http-endpoint",
            capability="web.http.inspect",
            operation="inspect",
            tool_id=ToolId("web.http.inspect"),
            target_bind="endpoint",
            preconditions=[NodePattern(type=GraphNodeType.ENDPOINT, bind="endpoint")],
            reason="Inspect a discovered endpoint before proposing deeper tests.",
            hypothesis=HypothesisDraft(
                description="The endpoint exposes behavior that can be analyzed safely.",
                confidence=0.3,
            ),
            observed_attrs={"web_inspected": True},
        ),
        PlanStep(
            step_id="inspect-http-service",
            capability="web.http.inspect",
            operation="inspect",
            tool_id=ToolId("web.http.inspect"),
            target_bind="service",
            preconditions=[NodePattern(type=GraphNodeType.SERVICE, bind="service")],
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
    capabilities=["mobile.artifact.analyze"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="analyze-mobile-artifact",
            capability="mobile.artifact.analyze",
            operation="analyze",
            tool_id=ToolId("mobile.artifact.analyze"),
            target_bind="asset",
            preconditions=[NodePattern(type=GraphNodeType.ASSET, bind="asset")],
            reason="Extract security-relevant structure from the discovered mobile artifact.",
            hypothesis=HypothesisDraft(
                description="The artifact contains security-relevant components or endpoints.",
                confidence=0.3,
            ),
            observed_attrs={"mobile_analyzed": True},
        )
    ],
)
