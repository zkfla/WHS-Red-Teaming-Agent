"""Deterministic progress signals derived from a graph observation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import GraphEdgeType, GraphNodeType


async def canonical_progress_fingerprint(deps: Any, runtime: Any, *, step: int) -> str | None:
    """Shared OBSERVE/resume signal from uncapped, same-run original records.

    None means the read failed, not that progress occurred or stopped.
    """
    from redteam.attack_graph.service import AttackGraphService
    from redteam.orchestration.knowledge import collect_progress_content

    content = await collect_progress_content(
        AttackGraphService(deps.graph_store),
        deps.evidence_store,
        str(runtime.run_id),
        step=step,
        engagement_id=str(runtime.engagement_id),
    )
    if content is None:
        return None
    try:
        path = await deps.path_store.get_active(str(runtime.run_id))
    except Exception:  # noqa: BLE001 - failed reads cannot wake or stall a run
        return None
    completed = (
        [
            (item.step_id, item.objective, item.outcome)
            for item in path.steps
            if item.status == "completed" and item.evidence_ids
        ]
        if path
        else []
    )
    payload = [content, sorted(completed)]
    return (
        "original-v1:"
        + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    )


async def canonical_graph_changed(deps: Any, runtime: Any) -> bool:
    """Whether the current canonical content differs from the stored fingerprint.

    The stored fingerprint is the content of an earlier turn, so a consumer
    that must judge the *current* turn asks this instead of trusting a counter
    observed before the turn ran. One judgment, shared by the stall consumers
    and the resume gate. A failed read and a legacy (incompatible) hash cannot
    prove change.
    """
    fingerprint = await canonical_progress_fingerprint(deps, runtime, step=runtime.step_count)
    return (
        fingerprint is not None
        and str(runtime.last_progress_fingerprint or "").startswith("original-v1:")
        and fingerprint != runtime.last_progress_fingerprint
    )


def knowledge_fingerprint(knowledge: dict) -> str:
    """Progress is content or supported conclusions, never a new receipt name."""
    ignored = {
        "evidence_id",
        "evidence_ids",
        "invocation_id",
        "action_id",
        "run_id",
        "created_at",
        "updated_at",
        "step",
        "summary",
        "source_refs",
        "reason",
        "first_seen_step",
        "last_seen_step",
        "provenance",
        "observed_at",
        "timestamp",
        "hypothesis_id",
        "finding_id",
    }

    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items() if key not in ignored}
        if isinstance(value, list):
            unique = {json.dumps(canonical(item), sort_keys=True, default=str) for item in value}
            return sorted(unique)
        return value

    hypothesis_section = knowledge.get("hypotheses", [])
    hypothesis_items = (
        hypothesis_section.get("items", [])
        if isinstance(hypothesis_section, dict)
        else hypothesis_section
    )
    hypotheses = [
        item
        for item in hypothesis_items
        if isinstance(item, dict)
        and item.get("status") in {"confirmed", "refuted", "rejected"}
        and (
            item.get("evidence_ids")
            or item.get("supporting_evidence_ids")
            or item.get("refuting_evidence_ids")
            or item.get("source_refs")
        )
    ]
    payload = canonical({"facts": knowledge.get("facts", []), "conclusions": hypotheses})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def observation_fingerprint(
    observation: GraphObservation,
    *,
    include_node_types: Iterable[GraphNodeType | str] | None = None,
    include_edge_types: Iterable[GraphEdgeType | str] = (),
    include_hypotheses: bool = True,
    include_findings: bool = True,
) -> str:
    """Hash meaningful graph state, optionally scoped to one agent role.

    include_edge_types is empty by default: a role that does not subscribe to
    relations must not be woken by an edge it never planned from. A role that
    does subscribe is woken by an edge-only change -- a new CAN_ACCESS
    between nodes it already knew -- which leaves every node id and node
    digest identical.
    """

    # Closed per-attempt hypotheses accumulate as audit records. Counting them
    # as progress lets a repeatable action manufacture novelty forever; active
    # hypotheses are represented separately below.
    ignored_types = {"hypothesis"}
    if not include_findings:
        ignored_types.add("finding")
    included = (
        {
            item.value if isinstance(item, GraphNodeType) else str(item)
            for item in include_node_types
        }
        if include_node_types is not None
        else None
    )

    def relevant(name: str) -> bool:
        return name not in ignored_types and (included is None or name in included)

    subscribed_edges = {
        item.value if isinstance(item, GraphEdgeType) else str(item) for item in include_edge_types
    }

    payload = {
        "observation_context_counts": {
            name: count
            for name, count in observation.observation_context_counts.items()
            if relevant(name)
        },
        "node_counts": {
            name: count for name, count in observation.node_counts.items() if relevant(name)
        },
        "nodes_by_type": {
            name: labels for name, labels in observation.nodes_by_type.items() if relevant(name)
        },
        "open_hypotheses": observation.open_hypotheses if include_hypotheses else [],
        "findings": observation.findings if include_findings else [],
        # Content, not just identity: a role must be woken by a changed fact
        # on a node it already knows (endpoint 401 -> 403, a newly observed
        # response, a newly registered session) -- those leave node ids and
        # counts untouched.
        "content_digests": {
            name: digest for name, digest in observation.content_digests.items() if relevant(name)
        },
        "edge_counts": {
            name: count
            for name, count in observation.edge_counts.items()
            if name in subscribed_edges
        },
        "relation_digests": {
            name: digest
            for name, digest in observation.relation_digests.items()
            if name in subscribed_edges
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]
