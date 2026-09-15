"""Structural checks: is this graph safe to report from?

Returns findings; the caller decides whether they block.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.attack_graph.cleanup import collect
from redteam.attack_graph.identity import is_well_formed, split_node_id
from redteam.attack_graph.predicates import Confidence, facts_of, provenance_of
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType


class IntegrityReport(BaseModel):
    """Everything wrong with the graph's record, grouped by kind."""

    ungrounded_findings: list[str] = Field(default_factory=list)
    unsourced_nodes: list[str] = Field(default_factory=list)
    weakly_grounded_nodes: list[str] = Field(default_factory=list)
    dangling_edges: list[str] = Field(default_factory=list)
    malformed_node_ids: list[str] = Field(default_factory=list)
    unresolved_hypotheses: list[str] = Field(default_factory=list)
    uncleaned_resources: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not any(
            [
                self.ungrounded_findings,
                self.unsourced_nodes,
                self.weakly_grounded_nodes,
                self.dangling_edges,
                self.malformed_node_ids,
                self.unresolved_hypotheses,
                self.uncleaned_resources,
            ]
        )

    @property
    def blocking(self) -> list[str]:
        """The subset that must be fixed before a report goes out."""
        return [
            *(f"ungrounded finding: {item}" for item in self.ungrounded_findings),
            *(f"dangling edge: {item}" for item in self.dangling_edges),
            *(f"malformed node id: {item}" for item in self.malformed_node_ids),
            # A run still holding a resource it created has changed the
            # target system and not finished saying so.
            *(f"uncleaned resource: {item}" for item in self.uncleaned_resources),
        ]

    def describe(self) -> str:
        parts = [
            f"{name}={len(value)}"
            for name, value in self.model_dump().items()
            if isinstance(value, list) and value
        ]
        return ", ".join(parts) or "clean"


def check_integrity(nodes: list[GraphNode], edges: list[GraphEdge]) -> IntegrityReport:
    report = IntegrityReport()
    known_ids = {node.id for node in nodes}

    for node in nodes:
        node_id = str(node.id)

        if not is_well_formed(node.id):
            report.malformed_node_ids.append(node_id)
        else:
            declared_type, _ = split_node_id(node.id)
            if declared_type != node.type:
                report.malformed_node_ids.append(
                    f"{node_id} (id says {declared_type.value}, node says {node.type.value})"
                )

        provenance = provenance_of(node.properties)
        if not provenance:
            report.unsourced_nodes.append(node_id)
        elif all(
            entry.get("confidence", Confidence.INFERRED.value) == Confidence.INFERRED.value
            for entry in provenance
        ):
            report.weakly_grounded_nodes.append(node_id)

        facts = facts_of(node.properties)
        if node.type == GraphNodeType.FINDING and facts.get("verified") is True:
            if not facts.get("evidence_ids"):
                report.ungrounded_findings.append(
                    f"{facts.get('finding_id', node_id)}: verified without evidence"
                )
        if node.type == GraphNodeType.HYPOTHESIS and facts.get("status") == "open":
            report.unresolved_hypotheses.append(str(facts.get("hypothesis_id", node_id)))

    for item in collect(nodes).outstanding:
        report.uncleaned_resources.append(
            f"{item.node_id} ({item.resource_type}, {item.status.value})"
        )

    for edge in edges:
        missing = [
            str(endpoint)
            for endpoint in (edge.source_id, edge.target_id)
            if endpoint not in known_ids
        ]
        if missing:
            report.dangling_edges.append(f"{edge.id} -> missing {missing}")

    return report
