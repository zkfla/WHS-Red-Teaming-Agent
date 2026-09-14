"""Evidence Store port. Concrete stores in ``redteam.evidence.stores``.

Append-only by convention: ``append`` is the only write method.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.ids import ActionId, EvidenceId, RunId


class EvidenceStore(Protocol):
    async def append(self, evidence: EvidenceRecord) -> None: ...

    async def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None: ...

    async def list_for_run(self, run_id: RunId) -> list[EvidenceRecord]: ...

    async def list_for_action(self, action_id: ActionId) -> list[EvidenceRecord]: ...
