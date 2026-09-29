"""Bounded projection of the graph for a planner prompt.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from redteam.attack_graph.predicates import (
    attrs_of,
    facts_of,
    last_seen_step,
    predicate_flags,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId

ATTEMPTED_FACT_KEY = "attempted_operations"


#: Fact/attribute keys a planner or candidate provider actually plans from.
#: A node's *identity* alone is not enough to decide whether a role has
#: something new to do -- an endpoint that was 401 anonymously and is now
#: 403 for the same session has the same id, the same context key and the
#: same counts, yet is a genuinely different planning input. This list is
#: a deliberate allow-list rather than "hash everything": volatile
#: bookkeeping (last observed step, accumulated evidence ids, the attempt
#: log, provenance) changes on every turn and would make every role look
#: reactivatable for no informational reason.
PLANNING_CONTENT_KEYS = (
    "observations",
    "path",
    "method",
    "host",
    "port",
    "scheme",
    "application_protocol",
    "service_name",
    "target_ref",
    "asset_ref",
    "apk_ref",
    "project_dir",
    "in_scope",
    "authenticated",
    "auth_session_config",
    "path_parameters",
    "status_code",
    "redirect",
    "object_scoped",
    "web_inspected",
    "analysis_decoded",
    "manifest_analyzed",
    "smali_inventoried",
    "sensitive_strings_searched",
    "responded",
    "reachable",
    "probed",
    "assessment_kind",
)

#: Edge property keys that are volatile bookkeeping rather than relation
#: content. A relation digest exists so a *meaningful* access change wakes a
#: role -- a new CAN_ACCESS, a changed outcome. Including provenance, step
#: counters or timestamps would instead make every edge look changed on every
#: turn and wake every subscribed role for no informational reason.
VOLATILE_RELATION_KEYS = frozenset(
    {
        "provenance",
        "step",
        "first_seen_step",
        "last_seen_step",
        "created_at",
        "updated_at",
        "timestamp",
        "run_id",
        "evidence_id",
        "evidence_ids",
        "retractions",
    }
)


def _planning_content(node: GraphNode) -> str:
    """Digest of one node's planning-relevant content, stable across bookkeeping."""

    facts = facts_of(node.properties)
    attrs = attrs_of(node.properties)
    payload = {
        "facts": {key: facts[key] for key in PLANNING_CONTENT_KEYS if key in facts},
        "attrs": {key: attrs[key] for key in PLANNING_CONTENT_KEYS if key in attrs},
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _relation_content(edge: GraphEdge) -> str:
    """Digest of one edge's stable relation content.

    The endpoints and the non-volatile properties are included; the edge id
    and its creation time are not, so re-materialising an identical relation
    does not read as a change.
    """

    properties = edge.properties if isinstance(edge.properties, dict) else {}
    stable = {
        str(key): properties[key]
        for key in sorted(properties, key=str)
        if str(key) not in VOLATILE_RELATION_KEYS
    }
    payload = {
        "source": str(edge.source_id),
        "target": str(edge.target_id),
        "properties": stable,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


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
    observation_context_counts: dict[str, int] = Field(default_factory=dict)
    #: Per node type, a digest over every node's PLANNING_CONTENT_KEYS
    #: content. Lets a role-scoped revision fingerprint notice a *changed fact*
    #: (a status code, a new observation, a security-header flag, a newly
    #: registered session) instead of only a changed node identity.
    content_digests: dict[str, str] = Field(default_factory=dict)
    #: How many edges of each type the graph holds right now.
    edge_counts: dict[str, int] = Field(default_factory=dict)
    #: Per edge type, a digest over every edge's stable relation content.
    #: Lets a role notice an edge-only change -- a CAN_ACCESS appearing
    #: between principals and resources the graph already knew about -- which
    #: leaves every node id and every node digest untouched.
    relation_digests: dict[str, str] = Field(default_factory=dict)

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
    edges: list[GraphEdge] | None = None,
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
    content: dict[str, list[str]] = {}
    for node in nodes:
        node_counts[node.type.value] = node_counts.get(node.type.value, 0) + 1
        # Over *all* nodes, not the truncated display bucket: truncation is a
        # prompt-size decision and must not hide a content change from the
        # reactivation fingerprint.
        content.setdefault(node.type.value, []).append(
            f"{node.id}={_planning_content(node)}"
        )
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

    contexts: dict[str, set[tuple[str, str]]] = {}
    for node in nodes:
        for item in facts_of(node.properties).get("observations", []):
            if isinstance(item, dict) and isinstance(item.get("context_key"), str):
                contexts.setdefault(node.type.value, set()).add((str(node.id), item["context_key"]))

    edge_counts: dict[str, int] = {}
    relations: dict[str, list[str]] = {}
    for edge in edges or []:
        type_name = edge.type.value
        edge_counts[type_name] = edge_counts.get(type_name, 0) + 1
        relations.setdefault(type_name, []).append(_relation_content(edge))

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
        observation_context_counts={kind: len(keys) for kind, keys in contexts.items()},
        content_digests={
            name: hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:16]
            for name, items in content.items()
        },
        edge_counts=edge_counts,
        relation_digests={
            name: hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:16]
            for name, items in relations.items()
        },
    )
