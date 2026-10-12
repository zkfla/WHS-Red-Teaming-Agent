"""Graph merge rules and optional store-owned, synchronous row updates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from weakref import WeakKeyDictionary

from redteam.attack_graph.predicates import (
    CLAIM_FACT_PREFIX,
    CLAIM_PROVENANCE_KEY,
    MAX_PROVENANCE_ENTRIES,
    claim_provenance_of,
    facts_of,
    merge_properties,
    retain_claim_confirmation,
    validate_properties,
    values_equal,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.core.ports.attack_graph import AttackGraphStore

NodeUpdate = Callable[[GraphNode | None], GraphNode | None]
EdgeUpdate = Callable[[GraphEdge | None], GraphEdge | None]


class NodeTypeConflictError(ValueError):
    """A write would change the meaning of an existing node ID."""


class UngroundedFindingError(ValueError):
    """A verified Finding must carry original evidence references."""


def validate_node(node: GraphNode) -> GraphNode:
    node = GraphNode.model_validate(node.model_dump())
    validate_properties(node.properties)
    if node.type is GraphNodeType.FINDING:
        facts = facts_of(node.properties)
        if facts.get("verified") and not facts.get("evidence_ids"):
            raise UngroundedFindingError("verified finding requires evidence_ids")
    return node


def merge_node(existing: GraphNode | None, incoming: GraphNode) -> GraphNode:
    incoming = validate_node(incoming)
    if existing is None:
        return incoming
    if existing.type is not incoming.type:
        raise NodeTypeConflictError(
            f"{incoming.id} is a {existing.type.value} node; refusing to upsert "
            f"it as {incoming.type.value}"
        )
    labels = sorted(set(existing.labels) | set(incoming.labels))
    properties = merge_properties(existing.properties, incoming.properties)
    if labels == existing.labels and properties == existing.properties:
        return existing
    return validate_node(existing.model_copy(update={
        "labels": labels, "properties": properties, "updated_at": incoming.updated_at,
    }))


def merge_edge(existing: GraphEdge | None, incoming: GraphEdge) -> GraphEdge:
    incoming = GraphEdge.model_validate(incoming.model_dump())
    validate_properties(incoming.properties)
    if existing is None:
        return incoming
    if (existing.type, existing.source_id, existing.target_id) != (
        incoming.type, incoming.source_id, incoming.target_id,
    ):
        raise ValueError("graph_edge_identity_conflict")
    properties = {**existing.properties, **incoming.properties}
    if "provenance" in incoming.properties:
        recorded = list(existing.properties.get("provenance", []))
        for entry in incoming.properties["provenance"]:
            if entry not in recorded:
                recorded.append(entry)
        properties["provenance"] = (
            recorded if len(recorded) <= MAX_PROVENANCE_ENTRIES
            else [recorded[0], *recorded[-(MAX_PROVENANCE_ENTRIES - 1):]]
        )
    claims = dict(claim_provenance_of(existing.properties))
    for key, entry in claim_provenance_of(incoming.properties).items():
        prop_key = key[len(CLAIM_FACT_PREFIX):] if key.startswith(CLAIM_FACT_PREFIX) else None
        if (prop_key is not None and prop_key in existing.properties
                and prop_key in incoming.properties and key in claims
                and values_equal(existing.properties[prop_key], incoming.properties[prop_key])):
            claims[key] = retain_claim_confirmation(claims[key], entry)
        else:
            claims[key] = entry
    if claims or CLAIM_PROVENANCE_KEY in incoming.properties:
        properties[CLAIM_PROVENANCE_KEY] = claims
    validate_properties(properties)
    return existing.model_copy(update={"properties": properties})


# Legacy stores retain in-process serialization. A persistent store's optional
# mutate_* contract owns the database transaction and rereads the latest row.
_STORE_LOCKS: WeakKeyDictionary[Any, list[asyncio.Lock]] = WeakKeyDictionary()


def _lock(store: Any, identity: str) -> asyncio.Lock:
    try:
        stripes = _STORE_LOCKS[store]
    except (KeyError, TypeError):
        stripes = [asyncio.Lock() for _ in range(64)]
        try:
            _STORE_LOCKS[store] = stripes
        except TypeError:
            pass
    return stripes[hash(identity) % len(stripes)]


async def update_node(
    store: AttackGraphStore, node_id: GraphNodeId, update: NodeUpdate,
) -> GraphNode | None:
    atomic = getattr(store, "mutate_node", None)
    if callable(atomic):
        return await atomic(node_id, update)
    async with _lock(store, str(node_id)):
        previous = await store.get_node(node_id)
        result = update(previous)
        if result is not None and result != previous:
            await store.add_node(result)
        return result


async def update_edge(
    store: AttackGraphStore, edge_id: GraphEdgeId, update: EdgeUpdate,
) -> GraphEdge | None:
    atomic = getattr(store, "mutate_edge", None)
    if callable(atomic):
        return await atomic(edge_id, update)
    async with _lock(store, str(edge_id)):
        previous = await store.get_edge(edge_id)
        result = update(previous)
        if result is not None and result != previous:
            await store.add_edge(result)
        return result
