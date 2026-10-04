"""Tool Result -> Attack Graph Mutation boundary.

    ToolResult (+ target/arguments, tool-identified)
        v
    ResultMapper            (per tool_id: parse output, decide what was
                              actually discovered)
        v
    GraphMutation            (nodes/edges to upsert -- not yet written)
        v
    orchestration.nodes.update  (writes them via AttackGraphService,
                                  which already deduplicates by
                                  deterministic node id)

Deliberately separate from ``orchestration.nodes.update`` so that module
never grows a ``if tool_id == "...": ...`` ladder: adding a new tool's
graph-enrichment logic means registering one more mapper here, not
editing UPDATE. A tool with no registered mapper keeps its raw evidence
available for review. Planning hints and expected observations never
become measured facts.
"""

from __future__ import annotations

from redteam.orchestration.observations.mapper import (
    RESULT_MAPPER_KIND,
    EdgeMutation,
    FindingMutation,
    GraphMutation,
    MapperContribution,
    MappingOutcome,
    NodeMutation,
    ResultMapperRegistry,
    default_result_mapper_registry,
    load_mapper_contribution,
    mapper_contribution,
    mapper_declarations_by_contribution_id,
    register_contribution,
    resolve_contribution_mapper,
)
from redteam.orchestration.observations.packages import (
    builtin_extension_declaration,
    builtin_observation_packages,
    mobsf_observation_package,
)
from redteam.orchestration.observations.public_lookup import (
    map_public_page_read,
    map_public_search_results,
)
from redteam.orchestration.observations.reference_scan import (
    TargetReference,
    is_absolute_url,
    is_reference_grounded,
    reference_address,
)

__all__ = [
    "EdgeMutation",
    "GraphMutation",
    "FindingMutation",
    "NodeMutation",
    "ResultMapperRegistry",
    "default_result_mapper_registry",
    "MapperContribution",
    "MappingOutcome",
    "TargetReference",
    "register_contribution",
    "resolve_contribution_mapper",
    "mapper_contribution",
    "load_mapper_contribution",
    "mapper_declarations_by_contribution_id",
    "RESULT_MAPPER_KIND",
    "builtin_observation_packages",
    "mobsf_observation_package",
    "builtin_extension_declaration",
    "map_public_search_results",
    "map_public_page_read",
    "is_absolute_url",
    "is_reference_grounded",
    "reference_address",
]
