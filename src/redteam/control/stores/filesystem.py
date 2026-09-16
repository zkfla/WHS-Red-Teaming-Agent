"""Filesystem-backed ``AuditStore``.

Append-only, one JSONL file per run (``<root>/<run_id>.jsonl``) --
mirrors ``redteam.evidence.stores.filesystem.FilesystemEvidenceStore``'s
shape and guarantees. Kept as a separate class rather than reusing
``FilesystemEvidenceStore`` directly: ``AuditEntry`` and
``EvidenceRecord`` are different domain concerns (one Control-team-
internal, one a frozen core contract) and collapsing them into one
class would create exactly the kind of accidental coupling the
dependency rules are meant to avoid, even though the on-disk shape
happens to be identical today.

TODO(control-team): ``list_for_action`` scans every run file, same
known limitation as ``FilesystemEvidenceStore`` -- add an index if/when
this becomes a bottleneck. No retention policy yet either.
"""

from __future__ import annotations

from pathlib import Path

from redteam.control.audit import AuditEntry
from redteam.core.ids import ActionId, RunId


class FilesystemAuditStore:
    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for_run(self, run_id: RunId) -> Path:
        return self._root / f"{run_id}.jsonl"

    async def append(self, entry: AuditEntry) -> None:
        path = self._path_for_run(entry.run_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")

    async def list_for_run(self, run_id: RunId) -> list[AuditEntry]:
        path = self._path_for_run(run_id)
        if not path.exists():
            return []
        return [
            AuditEntry.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def list_for_action(self, action_id: ActionId) -> list[AuditEntry]:
        results = []
        for path in self._root.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = AuditEntry.model_validate_json(line)
                if entry.action_id == action_id:
                    results.append(entry)
        return results
