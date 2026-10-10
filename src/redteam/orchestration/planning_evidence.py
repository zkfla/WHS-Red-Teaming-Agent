"""Fence append-only evidence writes, without replacing the owning store."""

import asyncio

from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.planning_admission import PlanningAdmission


class PlanningEvidenceStore:
    def __init__(self, store: EvidenceStore, admission: PlanningAdmission) -> None:
        self.store, self.admission = store, admission
        self._writer_lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self.store, name)

    async def append(self, evidence):
        from redteam.orchestration.evidence_validity import is_binding

        async with self._writer_lock:
            existing = await self.store.get(evidence.evidence_id)
            if existing is not None:
                if existing != evidence:
                    raise StateConflict("immutable_evidence_id_conflict")
                return
            references = ["evidence", f"evidence:{evidence.evidence_id}"]
            if is_binding(evidence):
                references.append(f"validity:{evidence.metadata.get('claim_evidence_id', '')}")
            run_id = str(evidence.run_id)
            writer = await self.admission.begin_reference_update(run_id, references)
            # Failure/cancellation keeps the fence: an unacknowledged append
            # is not proof that the store did not accept it.
            await self.store.append(evidence)
            await self.admission.finish_reference_update(run_id, references, writer)
