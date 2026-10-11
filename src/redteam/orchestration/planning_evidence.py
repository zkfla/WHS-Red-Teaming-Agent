"""Fence append-only evidence writes, without replacing the owning store."""

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.ids import EvidenceId, RunId
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.planning_admission import (
    PlanningAdmission,
    ReferenceUpdate,
    ReferenceWriteProof,
)

_APPEND_PROOF_KIND = "evidence_append_v1"


class EvidenceWriteRecovery(BaseModel):
    """Fence recovery only; never permission to replay or release a Task."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    writer: str
    subject_id: str | None = None
    state: Literal["confirmed", "held"]
    reason_code: str


def _references_for(evidence: EvidenceRecord) -> tuple[str, ...]:
    from redteam.orchestration.evidence_validity import is_binding

    references = ["evidence", f"evidence:{evidence.evidence_id}"]
    if is_binding(evidence):
        references.append(f"validity:{evidence.metadata.get('claim_evidence_id', '')}")
    return tuple(sorted(references))


class PlanningEvidenceStore:
    def __init__(self, store: EvidenceStore, admission: PlanningAdmission) -> None:
        self.store, self.admission = store, admission
        self._writer_lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self.store, name)

    async def append(self, evidence: EvidenceRecord) -> None:
        evidence = evidence.model_copy(deep=True)
        async with self._writer_lock:
            existing = await self.store.get(evidence.evidence_id)
            if existing is not None:
                if existing != evidence:
                    raise StateConflict("immutable_evidence_id_conflict")
                return
            references = _references_for(evidence)
            run_id = str(evidence.run_id)
            writer = await self.admission.begin_reference_update(run_id, references, proof=
                ReferenceWriteProof(kind=_APPEND_PROOF_KIND, subject_id=str(evidence.evidence_id),
                                    content_hash=content_hash(evidence)))
            # Failure/cancellation keeps the fence: an unacknowledged append
            # is not proof that the store did not accept it.
            await self.store.append(evidence)
            await self.admission.finish_reference_update(run_id, references, writer)

    async def reconcile_pending_writes(
        self, run_id: str, *, supported_only: bool = False,
    ) -> tuple[EvidenceWriteRecovery, ...]:
        """Reconcile accepted immutable appends under the caller's Run ownership.

        The driver must hold exclusive Run ownership and have no active actors.
        No append, graph write, model, provider query, or tool call is performed.
        Legacy/other writer kinds, missing/changed originals, integrity errors,
        and changed CAS inputs remain fenced; elapsed time is not a proof.
        """
        async with self._writer_lock:
            outcomes = []
            for intent in await self.admission.pending_reference_updates(run_id):
                if supported_only and (
                    intent.proof is None or intent.proof.kind != _APPEND_PROOF_KIND
                ):
                    continue
                reason = await self._check_append(intent)
                if reason is None:
                    try:
                        await self.admission.finish_reference_update(
                            run_id, [key for key, _ in intent.references], intent.writer,
                            expected=intent,
                        )
                    except StateConflict:
                        reason = "reference_update_changed_during_recovery"
                outcomes.append(EvidenceWriteRecovery(
                    writer=intent.writer,
                    subject_id=intent.proof.subject_id if intent.proof else None,
                    state="held" if reason else "confirmed",
                    reason_code=reason or "original_evidence_append_confirmed",
                ))
            return tuple(outcomes)

    async def _check_append(self, intent: ReferenceUpdate) -> str | None:
        proof = intent.proof
        if proof is None:
            return "reference_update_proof_missing"
        if proof.kind != _APPEND_PROOF_KIND:
            return "reference_update_proof_unsupported"
        verify = getattr(self.store, "verify_run_integrity", None)
        if not callable(verify):
            return "evidence_store_integrity_unavailable"
        try:
            if await verify(RunId(intent.run_id)):
                return "evidence_store_integrity_failed"
            original = await self.store.get(EvidenceId(proof.subject_id))
        except Exception:  # noqa: BLE001 - unavailable never proves acceptance
            return "evidence_store_read_failed"
        if original is None:
            return "original_evidence_missing"
        if (str(original.run_id) != intent.run_id or str(original.evidence_id) != proof.subject_id
                or content_hash(original) != proof.content_hash):
            return "original_evidence_conflict"
        if tuple(key for key, _ in intent.references) != _references_for(original):
            return "evidence_reference_inventory_conflict"
        return None
