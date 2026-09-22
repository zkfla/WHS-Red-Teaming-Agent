"""Filesystem-backed ``EvidenceStore``.

Append-only by construction: each run gets one JSONL file
(``<root>/<run_id>.jsonl``) and records are only ever appended, never
rewritten in place. Good enough for the skeleton; a future store (S3,
object storage, ...) implements the same Protocol.

TODO(control-team): ``list_for_action`` currently scans every run file —
fine for the skeleton's scale, not for a real deployment; add an index
if/when this becomes a bottleneck.
"""

from __future__ import annotations

from pathlib import Path

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.ids import ActionId, EvidenceId, RunId


class FilesystemEvidenceStore:
    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for_run(self, run_id: RunId) -> Path:
        return self._root / f"{run_id}.jsonl"

    async def append(self, evidence: EvidenceRecord) -> None:
        path = self._path_for_run(evidence.run_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(evidence.model_dump_json() + "\n")

    async def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        for path in self._root.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = EvidenceRecord.model_validate_json(line)
                if record.evidence_id == evidence_id:
                    return record
        return None

    async def list_for_run(self, run_id: RunId) -> list[EvidenceRecord]:
        path = self._path_for_run(run_id)
        if not path.exists():
            return []
        return [
            EvidenceRecord.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def list_for_action(self, action_id: ActionId) -> list[EvidenceRecord]:
        results = []
        for path in self._root.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = EvidenceRecord.model_validate_json(line)
                if record.action_id == action_id:
                    results.append(record)
        return results
