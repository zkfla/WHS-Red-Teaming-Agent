"""Per-run activation guard for the APK static-analysis role."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from redteam.attack_graph.predicates import facts_of
from redteam.core.enums import GraphNodeType, ToolHealthStatus
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.orchestration.eligibility import (
    ELIGIBILITY_PROVIDER_KIND,
    EligibilityContext,
    EligibilityDecision,
    EligibilityScope,
)

ANALYSIS_AGENT_ID = "analysis-agent"
STATIC_ANALYSIS_CAPABILITY = "analysis"
STATIC_ANALYSIS_OPERATION = "static_analysis"


@dataclass(frozen=True)
class AnalysisActivation:
    active: bool
    reason: str
    apk_asset_node_ids: tuple[str, ...] = ()
    compatible_tool_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_static_analysis_tool(tool: Any) -> bool:
    """Return whether a descriptor is a healthy APK-compatible tool.

    The temporary compatibility definition agreed for Track A is exact:
    capability ``analysis``, operation ``static_analysis``, health
    ``HEALTHY``, and either an unconstrained target type or ``asset``.
    """

    return (
        STATIC_ANALYSIS_CAPABILITY in tool.capability_tags
        and STATIC_ANALYSIS_OPERATION in tool.operations
        and tool.health_status is ToolHealthStatus.HEALTHY
        and (not tool.target_types or "asset" in tool.target_types)
    )


async def analysis_activation(
    graph: Any,
    catalog: ToolCatalog | None,
) -> AnalysisActivation:
    assets = await graph.query(node_type=GraphNodeType.ASSET)
    apk_nodes = tuple(
        sorted(
            str(node.id)
            for node in assets
            if isinstance(facts_of(node.properties).get("apk_ref"), str)
            and facts_of(node.properties)["apk_ref"].strip()
        )
    )
    if not apk_nodes:
        return AnalysisActivation(False, "no_explicit_apk_asset")
    compatible = tuple(
        sorted(
            str(tool.tool_id)
            for tool in (catalog.list_by_capability("analysis") if catalog else [])
            if is_static_analysis_tool(tool)
        )
    )
    if not compatible:
        return AnalysisActivation(
            False,
            "no_healthy_static_analysis_tool",
            apk_asset_node_ids=apk_nodes,
        )
    return AnalysisActivation(
        True,
        "active",
        apk_asset_node_ids=apk_nodes,
        compatible_tool_ids=compatible,
    )


def analysis_role_visible(
    agent: Any,
    activation: AnalysisActivation,
    *,
    execution_ready: bool = False,
) -> bool:
    return str(agent.agent_id) != ANALYSIS_AGENT_ID or activation.active or execution_ready


def analysis_tool_visible(tool: Any, activation: AnalysisActivation) -> bool:
    if STATIC_ANALYSIS_OPERATION not in tool.operations:
        return True
    return activation.active and str(tool.tool_id) in activation.compatible_tool_ids


STATIC_ANALYSIS_ELIGIBILITY_ID = "eligibility:static-analysis"


class StaticAnalysisEligibilityProvider:
    """Built-in eligibility for the APK static-analysis role (M03).

    Same meaning as the module functions below: an asset counts when it
    carries a non-blank ``apk_ref`` fact; a tool counts when it is a
    healthy ``analysis``/``static_analysis`` tool bound to ``asset``
    targets. Delegates to those functions so the meaning cannot drift.
    """

    provider_id = STATIC_ANALYSIS_ELIGIBILITY_ID
    applies_to = EligibilityScope(
        capabilities=(STATIC_ANALYSIS_CAPABILITY,),
        operations=(STATIC_ANALYSIS_OPERATION,),
    )

    async def asset_scope(self, context: EligibilityContext) -> tuple[str, ...]:
        activation = await analysis_activation(context.graph, context.catalog)
        return activation.apk_asset_node_ids

    async def role_eligible(self, role_id: str, context: EligibilityContext) -> EligibilityDecision:
        activation = await analysis_activation(context.graph, context.catalog)
        if role_id != ANALYSIS_AGENT_ID:
            return EligibilityDecision(True, "provider_does_not_constrain_role")
        return EligibilityDecision(activation.active or context.execution_ready, activation.reason)

    async def tool_eligible(self, tool: Any, context: EligibilityContext) -> EligibilityDecision:
        activation = await analysis_activation(context.graph, context.catalog)
        if STATIC_ANALYSIS_OPERATION not in tool.operations:
            return EligibilityDecision(True, "provider_does_not_constrain_tool")
        if activation.active and str(tool.tool_id) in activation.compatible_tool_ids:
            return EligibilityDecision(True, "active")
        return EligibilityDecision(False, activation.reason)


def static_analysis_eligibility_contribution():
    """This provider in the composition root's declaration shape."""
    from redteam.core.extensions import Contribution

    return Contribution(
        kind=ELIGIBILITY_PROVIDER_KIND,
        contribution_id=STATIC_ANALYSIS_ELIGIBILITY_ID,
        version="1",
        factory=lambda _ctx: StaticAnalysisEligibilityProvider(),
    )


def builtin_extension_declaration():
    """Built-in eligibility as one declaration for the composition root."""
    from redteam.core.extensions import ExtensionDeclaration

    return ExtensionDeclaration(
        extension_id="builtin.analysis-eligibility",
        version="1",
        contributions=(static_analysis_eligibility_contribution(),),
    )
