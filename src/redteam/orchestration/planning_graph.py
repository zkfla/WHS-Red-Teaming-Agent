"""Fence graph mutations before I/O against specialist planning read sets.

The store remains canonical. Initial strategy input reads the whole graph, so
it declares the graph query set as a dependency. Narrow consumers can use node
and neighbor references instead; unrelated Task-only settlements do not bump
graph versions. A failed write leaves its fence for explicit reconciliation.
"""

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict

from redteam.attack_graph.stores.base import StoreCapabilityError, require_receipted
from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationReceipt,
    checked_receipt,
    mutation_hash,
)
from redteam.core.ids import new_id
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.metered_llm import current_request_scope
from redteam.orchestration.planning_admission import (
    PlanningAdmission,
    ReferenceUpdate,
    ReferenceWriteProof,
)

_MUTATION_PROOF_KIND = "graph_mutation_v1"


class GraphWriteRecovery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    writer: str
    subject_id: str | None = None
    state: Literal["confirmed", "held"]
    reason_code: str


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
        references = tuple(sorted(set(("graph", *references))))
        payload = {"node": value} if method == "add_node" else (
            {"edge": value} if method == "add_edge" else {"identity": str(value)}
        )
        mutation = GraphMutation(
            run_id=scope.run_id, mutation_id=new_id("graph_write"), operation=method,
            references=references, **payload,
        ).model_copy(deep=True)
        value = mutation.node if mutation.node is not None else (
            mutation.edge if mutation.edge is not None else mutation.identity
        )
        async with self._writer_lock:
            proof = None
            try:
                receipted = require_receipted(self.store)
                prepared = await receipted.prepare_graph_mutation(mutation)
                prepared = GraphMutation.model_validate(prepared.model_dump()).model_copy(deep=True)
                if (prepared.run_id != mutation.run_id
                        or prepared.mutation_id != mutation.mutation_id
                        or prepared.operation != mutation.operation
                        or prepared.references != mutation.references):
                    raise StateConflict("graph_mutation_preparation_conflict")
                mutation = prepared.model_copy(deep=True)
                proof = ReferenceWriteProof(
                    kind=_MUTATION_PROOF_KIND, subject_id=mutation.mutation_id,
                    content_hash=mutation_hash(mutation),
                )
            except StoreCapabilityError:
                # Older/custom providers retain their original write behavior.
                # An interrupted write cannot be recovered without a receipt.
                pass
            writer = await self.admission.begin_reference_update(
                scope.run_id, references, proof=proof,
            )
            if proof is None:
                result = await getattr(self.store, method)(value)
            else:
                receipt = checked_receipt(await receipted.apply_graph_mutation(mutation), mutation)
                result = receipt.result
            await self.admission.finish_reference_update(scope.run_id, references, writer)
            return result

    async def reconcile_pending_writes(
        self, run_id: str, *, supported_only: bool = False,
    ) -> tuple[GraphWriteRecovery, ...]:
        """Confirm durable original receipts under exclusive, quiescent Run ownership.

        No graph mutation/replay, task release, or equality check on current
        graph values occurs. Unknown/legacy writers remain fenced.
        """
        async with self._writer_lock:
            outcomes = []
            for intent in await self.admission.pending_reference_updates(run_id):
                if supported_only and (
                    intent.proof is None or intent.proof.kind != _MUTATION_PROOF_KIND
                ):
                    continue
                reason = await self._check_receipt(intent)
                if reason is None:
                    try:
                        await self.admission.finish_reference_update(
                            run_id, [key for key, _ in intent.references], intent.writer,
                            expected=intent,
                        )
                    except StateConflict:
                        reason = "reference_update_changed_during_recovery"
                outcomes.append(GraphWriteRecovery(
                    writer=intent.writer,
                    subject_id=intent.proof.subject_id if intent.proof else None,
                    state="held" if reason else "confirmed",
                    reason_code=reason or "original_graph_mutation_confirmed",
                ))
            return tuple(outcomes)

    async def _check_receipt(self, intent: ReferenceUpdate) -> str | None:
        proof = intent.proof
        if proof is None:
            return "reference_update_proof_missing"
        if proof.kind != _MUTATION_PROOF_KIND:
            return "reference_update_proof_unsupported"
        try:
            store = require_receipted(self.store)
        except StoreCapabilityError:
            return "graph_mutation_receipt_unavailable"
        try:
            receipt = await store.read_graph_mutation_receipt(intent.run_id, proof.subject_id)
            if receipt is not None:
                receipt = GraphMutationReceipt.model_validate(receipt.model_dump())
        except Exception:  # noqa: BLE001 - unavailable is not acceptance
            return "graph_mutation_receipt_read_failed"
        if receipt is None:
            return "original_graph_mutation_receipt_missing"
        if (receipt.run_id != intent.run_id or receipt.mutation_id != proof.subject_id
                or receipt.request_hash != proof.content_hash):
            return "original_graph_mutation_receipt_conflict"
        if receipt.references != tuple(key for key, _ in intent.references):
            return "graph_reference_inventory_conflict"
        return None

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
