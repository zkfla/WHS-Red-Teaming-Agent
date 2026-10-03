"""Per-run activation guard for the APK static-analysis role."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from redteam.attack_graph.predicates import facts_of
from redteam.core.enums import GraphNodeType, ToolHealthStatus
from redteam.core.ports.tool_catalog import ToolCatalog

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
    return (
        str(agent.agent_id) != ANALYSIS_AGENT_ID
        or activation.active
        or execution_ready
    )


def analysis_tool_visible(tool: Any, activation: AnalysisActivation) -> bool:
    if STATIC_ANALYSIS_OPERATION not in tool.operations:
        return True
    return activation.active and str(tool.tool_id) in activation.compatible_tool_ids
