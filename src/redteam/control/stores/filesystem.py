"""Filesystem-backed ``AuditStore``/``ApprovalStore``.

``FilesystemAuditStore``: append-only, one JSONL file per run
(``<root>/<run_id>.jsonl``).

``FilesystemApprovalStore``: one JSON file per approval
(``<root>/<approval_id>.json``), overwritten in place -- an Approval is
a single mutable record (PENDING -> APPROVED/REJECTED/EXPIRED), not a
log, so it doesn't fit the append-only shape.

Both mirror ``redteam.evidence.stores.filesystem.FilesystemEvidenceStore``'s
guarantees but are kept as separate classes: ``AuditEntry``/``Approval``
and ``EvidenceRecord`` are different domain concerns (two Control-team-
internal, one a frozen core contract), and collapsing them into one
class would create exactly the kind of accidental coupling the
dependency rules are meant to avoid, even where the on-disk shape
happens to look similar today.

TODO(control-team): ``list_for_action``/``list_pending`` scan every
file in ``root`` -- same known limitation as
``FilesystemEvidenceStore``; add an index if/when this becomes a
bottleneck. No retention policy on the audit side yet either.
"""

from __future__ import annotations

from pathlib import Path

from redteam.control.audit import AuditEntry
from redteam.core.contracts.policy import Approval
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ActionId, ApprovalId, RunId


class FilesystemAuditStore:
    def __init__(self, root_dir: str | Path, *, create: bool = True) -> None:
        self._root = Path(root_dir)
        if create:
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


class FilesystemApprovalStore:
    def __init__(self, root_dir: str | Path, *, create: bool = True) -> None:
        self._root = Path(root_dir)
        if create:
            self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, approval_id: ApprovalId) -> Path:
        return self._root / f"{approval_id}.json"

    async def save(self, approval: Approval) -> None:
        self._path_for(approval.approval_id).write_text(
            approval.model_dump_json(), encoding="utf-8"
        )

    async def get(self, approval_id: ApprovalId | str) -> Approval | None:
        path = self._path_for(ApprovalId(str(approval_id)))
        if not path.exists():
            return None
        return Approval.model_validate_json(path.read_text(encoding="utf-8"))

    async def list_pending(self) -> list[Approval]:
        results = []
        for path in self._root.glob("*.json"):
            approval = Approval.model_validate_json(path.read_text(encoding="utf-8"))
            if approval.status == ApprovalStatus.PENDING:
                results.append(approval)
        return results
