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
editing UPDATE. A tool with no registered mapper changes nothing about
today's behavior (only the static ``PlanStep.observed_attrs`` on the
directly-targeted node, as before).
"""

from __future__ import annotations

from redteam.orchestration.observations.mapper import (
    EdgeMutation,
    FindingMutation,
    GraphMutation,
    NodeMutation,
    ResultMapperRegistry,
    default_result_mapper_registry,
)

__all__ = [
    "EdgeMutation",
    "GraphMutation",
    "FindingMutation",
    "NodeMutation",
    "ResultMapperRegistry",
    "default_result_mapper_registry",
]
