"""Deterministic progress signals derived from a graph observation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import GraphNodeType


def observation_fingerprint(
    observation: GraphObservation,
    *,
    include_node_types: Iterable[GraphNodeType | str] | None = None,
    include_hypotheses: bool = True,
    include_findings: bool = True,
) -> str:
    """Hash meaningful graph state, optionally scoped to one agent role."""

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

    payload = {
        "observation_context_counts": {
            name: count for name, count in observation.observation_context_counts.items()
            if relevant(name)
        },
        "node_counts": {
            name: count
            for name, count in observation.node_counts.items()
            if relevant(name)
        },
        "nodes_by_type": {
            name: labels
            for name, labels in observation.nodes_by_type.items()
            if relevant(name)
        },
        "open_hypotheses": observation.open_hypotheses if include_hypotheses else [],
        "findings": observation.findings if include_findings else [],
        # Content, not just identity: a role must be woken by a changed fact
        # on a node it already knows (endpoint 401 -> 403, a newly observed
        # response, a newly registered session) -- those leave node ids and
        # counts untouched.
        "content_digests": {
            name: digest
            for name, digest in observation.content_digests.items()
            if relevant(name)
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]
