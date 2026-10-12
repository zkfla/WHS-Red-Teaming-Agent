"""Explicit storage fixtures, replacing tests of retired graph export/import APIs.

These helpers seed detached in-memory stores using the actual internal record
parser. They are never installed in application assembly or a migration tool.
"""

from typing import Any

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.snapshot import from_records, to_records


async def records_for(graph: AttackGraphService, *, metadata=None) -> list[dict[str, Any]]:
    return to_records(await graph.list_nodes(), await graph.list_edges(), metadata=metadata)


async def seed_records(graph: AttackGraphService, records, *, merge=False) -> dict[str, Any]:
    nodes, edges, metadata = from_records(records)
    for node in nodes:
        if merge:
            await graph.upsert_node(node.type, node.properties, node_id=node.id,
                                    labels=list(node.labels), step=None)
        else:
            await graph._store.add_node(node)
    for edge in edges:
        await graph._store.add_edge(edge)
    return metadata
