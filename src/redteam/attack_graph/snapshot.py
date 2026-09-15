"""Append-only JSONL snapshot of the graph, for replay and reporting.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from typing import Any

from redteam.core.contracts.graph import GraphEdge, GraphNode

SNAPSHOT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

COLLECTION_KEY = "_collection"
META_COLLECTION = "graph_meta"
NODES_COLLECTION = "nodes"
EDGES_COLLECTION = "edges"


class SnapshotError(ValueError):
    """Raised when a snapshot cannot be read without losing information."""


def to_records(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [
        {
            COLLECTION_KEY: META_COLLECTION,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            **(metadata or {}),
        }
    ]
    for node in sorted(nodes, key=lambda item: str(item.id)):
        records.append({COLLECTION_KEY: NODES_COLLECTION, **node.model_dump(mode="json")})
    for edge in sorted(edges, key=lambda item: (item.created_at, str(item.id))):
        records.append({COLLECTION_KEY: EDGES_COLLECTION, **edge.model_dump(mode="json")})
    return records


def from_records(
    records: list[dict[str, Any]],
) -> tuple[list[GraphNode], list[GraphEdge], dict[str, Any]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    metadata: dict[str, Any] = {}
    seen_meta = False

    for record in records:
        collection = record.get(COLLECTION_KEY)
        body = {key: value for key, value in record.items() if key != COLLECTION_KEY}

        if collection == META_COLLECTION:
            seen_meta = True
            version = body.pop("schema_version", None)
            if version not in SUPPORTED_SCHEMA_VERSIONS:
                raise SnapshotError(
                    f"unsupported snapshot schema version {version!r} "
                    f"(this reader supports {SUPPORTED_SCHEMA_VERSIONS})"
                )
            metadata = body
        elif collection == NODES_COLLECTION:
            nodes.append(GraphNode.model_validate(body))
        elif collection == EDGES_COLLECTION:
            edges.append(GraphEdge.model_validate(body))
        else:
            raise SnapshotError(
                f"unknown collection {collection!r} — refusing to drop records this "
                f"reader does not understand"
            )

    # Without the header there is nothing to version-check against, so a
    # snapshot written by a newer schema would load silently and wrongly.
    if records and not seen_meta:
        raise SnapshotError(
            f"snapshot has no {META_COLLECTION!r} record, so its schema version cannot "
            f"be checked"
        )

    return nodes, edges, metadata


def round_trip_ok(nodes: list[GraphNode], edges: list[GraphEdge]) -> bool:
    """``export == export(import(export))`` — the property replay relies on."""
    exported = to_records(nodes, edges)
    reloaded_nodes, reloaded_edges, _ = from_records(exported)
    return to_records(reloaded_nodes, reloaded_edges) == exported
