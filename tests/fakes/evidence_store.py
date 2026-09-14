"""``FakeEvidenceStore`` — test double for ``redteam.core.ports.evidence_store.EvidenceStore``.

NOT a production implementation — see ``tests/fakes/__init__.py``. The
real store is ``redteam.evidence.stores.filesystem.FilesystemEvidenceStore``.
"""

from __future__ import annotations

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.ids import ActionId, EvidenceId, RunId


class FakeEvidenceStore:
    """Pure in-memory list — no filesystem, no JSONL, resets every test."""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    async def append(self, evidence: EvidenceRecord) -> None:
        self.records.append(evidence)

    async def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        return next((r for r in self.records if r.evidence_id == evidence_id), None)

    async def list_for_run(self, run_id: RunId) -> list[EvidenceRecord]:
        return [r for r in self.records if r.run_id == run_id]

    async def list_for_action(self, action_id: ActionId) -> list[EvidenceRecord]:
        return [r for r in self.records if r.action_id == action_id]
