"""Fence graph mutations before I/O against specialist planning read sets.

The store remains canonical. Initial strategy input reads the whole graph, so
it declares the graph query set as a dependency. Narrow consumers can use node
and neighbor references instead; unrelated Task-only settlements do not bump
graph versions. A failed write leaves its fence for explicit reconciliation.
"""

import asyncio

from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.orchestration.metered_llm import current_request_scope
from redteam.orchestration.planning_admission import PlanningAdmission


class PlanningGraphStore:
    def __init__(self, store: AttackGraphStore, admission: PlanningAdmission) -> None:
        self.store, self.admission = store, admission
        self._writer_lock = asyncio.Lock()

    def __getattr__(self, name):
        # Optional list/read capabilities keep the wrapped store's contracts.
        return getattr(self.store, name)

    async def _write(self, method, value, references):
        scope = current_request_scope()
        if scope is None:
            # Bootstrap happens before the first model request. It cannot race
            # an admitted strategy; active runners always carry Run scope.
            return await getattr(self.store, method)(value)
        references = ("graph", *references)
        async with self._writer_lock:
            writer = await self.admission.begin_reference_update(scope.run_id, references)
            result = await getattr(self.store, method)(value)
            await self.admission.finish_reference_update(scope.run_id, references, writer)
            return result

    async def add_node(self, node):
        return await self._write("add_node", node, (f"node:{node.id}",))

    async def add_edge(self, edge):
        return await self._write("add_edge", edge, (
            f"node:{edge.source_id}", f"node:{edge.target_id}",
            f"neighbors:{edge.source_id}", f"neighbors:{edge.target_id}",
        ))

    async def remove_node(self, node_id):
        return await self._write("remove_node", node_id, (f"node:{node_id}",))

    async def remove_edge(self, edge_id):
        edge = await self.store.get_edge(edge_id)
        references = () if edge is None else (
            f"node:{edge.source_id}", f"node:{edge.target_id}",
            f"neighbors:{edge.source_id}", f"neighbors:{edge.target_id}",
        )
        return await self._write("remove_edge", edge_id, references)
