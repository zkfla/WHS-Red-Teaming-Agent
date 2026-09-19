"""Bounded projection of the graph for a planner prompt.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.attack_graph.predicates import facts_of, last_seen_step, predicate_flags
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId

ATTEMPTED_FACT_KEY = "attempted_operations"


DEFAULT_MAX_PER_TYPE = 20
DEFAULT_MAX_TOTAL = 80
#: The attempt list needs a ceiling too, or a long run adds one prompt
#: line per action forever.
DEFAULT_MAX_ATTEMPTED = 40


class GraphObservation(BaseModel):
    """What the graph looks like right now, small enough to put in a prompt."""

    step: int = 0
    node_counts: dict[str, int] = Field(default_factory=dict)
    #: Display only. Never parse ids out of these — a key can contain the
    #: flag separator (``?q=a+b``). Use :attr:`node_ids_by_type`.
    nodes_by_type: dict[str, list[str]] = Field(default_factory=dict)
    #: Exact node ids, same order as ``nodes_by_type``.
    node_ids_by_type: dict[str, list[str]] = Field(default_factory=dict)
    open_hypotheses: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    attempted: list[str] = Field(default_factory=list)
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.node_counts

    def node_ids(self, node_type: GraphNodeType) -> list[GraphNodeId]:
        """Full ids for one type — the planner's handle back into the graph."""
        return [
            GraphNodeId(node_id) for node_id in self.node_ids_by_type.get(node_type.value, [])
        ]

    def has_attempted(self, operation_key: str) -> bool:
        return operation_key in self.attempted

    def summary_line(self) -> str:
        counts = ", ".join(f"{name}={count}" for name, count in sorted(self.node_counts.items()))
        return f"step={self.step} nodes[{counts or 'none'}] attempted={len(self.attempted)}"


def _node_label(node: GraphNode) -> str:
    _, _, key = str(node.id).partition(":")
    return f"{key or node.id}{predicate_flags(node.properties)}"


def build_observation(
    nodes: list[GraphNode],
    *,
    step: int = 0,
    max_per_type: int = DEFAULT_MAX_PER_TYPE,
    max_total: int = DEFAULT_MAX_TOTAL,
    max_attempted: int = DEFAULT_MAX_ATTEMPTED,
) -> GraphObservation:
    """Compress ``nodes`` into a planner-sized summary, most recently
    observed first."""
    ordered = sorted(
        nodes, key=lambda node: (-last_seen_step(node.properties), str(node.id))
    )

    nodes_by_type: dict[str, list[str]] = {}
    node_ids_by_type: dict[str, list[str]] = {}
    shown = 0
    truncated = False
    for node in ordered:
        type_name = node.type.value
        bucket = nodes_by_type.setdefault(type_name, [])
        node_ids_by_type.setdefault(type_name, [])
        if shown >= max_total or len(bucket) >= max_per_type:
            truncated = True
            continue
        bucket.append(_node_label(node))
        node_ids_by_type[type_name].append(str(node.id))
        shown += 1

    node_counts: dict[str, int] = {}
    open_hypotheses: list[str] = []
    findings: list[str] = []
    for node in nodes:
        node_counts[node.type.value] = node_counts.get(node.type.value, 0) + 1
        facts = facts_of(node.properties)

        if node.type == GraphNodeType.HYPOTHESIS and facts.get("status") == "open":
            open_hypotheses.append(
                f"{facts.get('hypothesis_id', node.id)}: {facts.get('description', '')}"
            )
        elif node.type == GraphNodeType.FINDING:
            verified = "verified" if facts.get("verified") else "unverified"
            severity = facts.get("severity", "info")
            findings.append(
                f"{facts.get('finding_id', node.id)} [{severity}/{verified}]"
            )

    # Recency order, then capped: the oldest attempts are the least useful
    # lines to spend prompt budget on.
    attempted: list[str] = []
    attempts_truncated = False
    for node in ordered:
        for entry in facts_of(node.properties).get(ATTEMPTED_FACT_KEY, []) or []:
            key = f"{entry}@{node.id}"
            if key in attempted:
                continue
            if len(attempted) >= max_attempted:
                attempts_truncated = True
                break
            attempted.append(key)
        if attempts_truncated:
            break

    return GraphObservation(
        step=step,
        node_counts=node_counts,
        nodes_by_type={name: labels for name, labels in nodes_by_type.items() if labels},
        node_ids_by_type={
            name: ids for name, ids in node_ids_by_type.items() if ids
        },
        open_hypotheses=sorted(open_hypotheses),
        findings=sorted(findings),
        attempted=sorted(attempted),
        truncated=truncated or attempts_truncated,
    )
