"""Filesystem-backed ``EvidenceStore``.

Append-only by construction: each run gets one JSONL file
(``<root>/<run_id>.jsonl``) and records are only ever appended, never
rewritten in place.

Beyond the frozen ``EvidenceStore`` Protocol (append/get/list_for_run/
list_for_action), this concrete class adds three things -- deliberately
*not* on the Protocol itself, per PLAN.md's PM Issue candidate #4
("EvidenceStore 포트에 조회 메서드 추가" -> "대안: concrete store에만
추가"), so no Contract Change Proposal was needed:

- An index (``_index.jsonl``: evidence_id/action_id/run_id/line_sha256)
  so ``get``/``list_for_action`` no longer scan every run file.
- ``verify_run_integrity``: compares indexed record IDs and line hashes
  against the run file, detecting changed, missing, and unindexed
  records. This is a different
  check from ``EvidenceRecord.digest`` (a PM-defined hash of the
  original ``ToolResult``, computed in ``core.convert`` before the
  record ever reaches this store) -- that field can't be re-verified
  here because the original ``ToolResult`` is intentionally never
  persisted alongside it (see ``core/contracts/evidence.py``: "Never
  embed large binaries directly"). What *can* be verified is whether
  the record has changed since this store wrote it, which is what
  ``verify_run_integrity`` checks.
- ``purge_older_than``: deletes whole run files (and their index
  entries) past a retention window, by file mtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.ids import ActionId, EvidenceId, RunId


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _integrity_problems(
    run_id: RunId, entries: list[dict[str, Any]], lines: list[str],
) -> list[EvidenceId]:
    expected = {}
    problems: list[EvidenceId] = []
    for entry in entries:
        if entry["run_id"] != run_id:
            continue
        identity = EvidenceId(entry["evidence_id"])
        if identity in expected and identity not in problems:
            problems.append(identity)
        expected[identity] = entry["line_hash"]
    seen: set[EvidenceId] = set()
    for line in lines:
        if not line.strip():
            continue
        record = EvidenceRecord.model_validate_json(line)
        identity = record.evidence_id
        if (identity in seen or identity not in expected or record.run_id != run_id
                or expected[identity] != _line_hash(line)) and identity not in problems:
            problems.append(identity)
        seen.add(identity)
    problems.extend(identity for identity in expected if identity not in seen)
    return problems


class EvidenceReadSnapshot:
    """Detached port view of an inspected Run, not a second durable store."""

    def __init__(
        self, records: list[EvidenceRecord], *, verified_run_id: RunId | None = None,
    ) -> None:
        self._records = tuple(record.model_copy(deep=True) for record in records)
        self._verified_run_id = verified_run_id

    async def verify_run_integrity(self, run_id: RunId) -> list[str]:
        if run_id != self._verified_run_id:
            raise ValueError("evidence_snapshot_integrity_not_inspected")
        return []  # Detached immutable copies of the inspected index/body pair.

    async def append(self, evidence: EvidenceRecord) -> None:
        raise RuntimeError("evidence_snapshot_is_read_only")

    async def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        return next((record.model_copy(deep=True) for record in self._records
                     if record.evidence_id == evidence_id), None)

    async def list_for_run(self, run_id: RunId) -> list[EvidenceRecord]:
        return [record.model_copy(deep=True) for record in self._records if record.run_id == run_id]

    async def list_for_action(self, action_id: ActionId) -> list[EvidenceRecord]:
        return [record.model_copy(deep=True) for record in self._records
                if record.action_id == action_id]


class FilesystemEvidenceStore:
    @staticmethod
    async def inspect_run(root_dir: str | Path, run_id: RunId) -> EvidenceReadSnapshot | None:
        """Read originals with index integrity, without constructor mkdir.

        File/index disagreement (including an interrupted append) is an
        error, never an empty successful inventory. This is not an
        authenticated index or an atomic snapshot with other stores.
        """
        identity = str(run_id)
        if not identity or identity in {".", ".."} or "/" in identity or "\\" in identity:
            raise ValueError("evidence_snapshot_run_id_invalid")
        root = Path(root_dir)
        path, index = root / f"{identity}.jsonl", root / "_index.jsonl"
        if not root.exists():
            return None
        if not root.is_dir():
            raise ValueError("evidence_snapshot_root_invalid")
        index_text = index.read_text(encoding="utf-8") if index.exists() else ""
        entries = [json.loads(line) for line in index_text.splitlines() if line.strip()]
        if not path.exists() and not any(entry["run_id"] == run_id for entry in entries):
            return None  # this directory does not establish this Run's evidence inventory
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if _integrity_problems(run_id, entries, text.splitlines()):
            raise ValueError("evidence_snapshot_integrity_failed")
        if (index.read_text(encoding="utf-8") if index.exists() else "") != index_text:
            raise ValueError("evidence_snapshot_changed")
        return EvidenceReadSnapshot([
            EvidenceRecord.model_validate_json(line) for line in text.splitlines() if line.strip()
        ], verified_run_id=run_id)

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_path = self._root / "_index.jsonl"

    def _path_for_run(self, run_id: RunId) -> Path:
        return self._root / f"{run_id}.jsonl"

    def _append_index(
        self, *, evidence_id: EvidenceId, action_id: ActionId | None, run_id: RunId, line_hash: str
    ) -> None:
        entry = {
            "evidence_id": evidence_id,
            "action_id": action_id,
            "run_id": run_id,
            "line_hash": line_hash,
        }
        with self._index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _read_index(self) -> list[dict[str, Any]]:
        if not self._index_path.exists():
            return []
        return [
            json.loads(line)
            for line in self._index_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def append(self, evidence: EvidenceRecord) -> None:
        line = evidence.model_dump_json()
        path = self._path_for_run(evidence.run_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._append_index(
            evidence_id=evidence.evidence_id,
            action_id=evidence.action_id,
            run_id=evidence.run_id,
            line_hash=_line_hash(line),
        )

    async def get(self, evidence_id: EvidenceId) -> EvidenceRecord | None:
        for entry in self._read_index():
            if entry["evidence_id"] == evidence_id:
                for record in await self.list_for_run(RunId(entry["run_id"])):
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
        run_ids = {
            entry["run_id"] for entry in self._read_index() if entry["action_id"] == action_id
        }
        results: list[EvidenceRecord] = []
        for run_id in run_ids:
            results.extend(
                record
                for record in await self.list_for_run(RunId(run_id))
                if record.action_id == action_id
            )
        return results

    async def verify_run_integrity(self, run_id: RunId) -> list[EvidenceId]:
        """Return IDs for changed, missing, duplicate, or unindexed records.

        The index and run file are compared in both directions. An empty
        result means they agree; it does not authenticate the index itself.
        """
        path = self._path_for_run(run_id)
        return _integrity_problems(
            run_id, self._read_index(),
            path.read_text(encoding="utf-8").splitlines() if path.exists() else [],
        )

    async def purge_older_than(self, days: float) -> list[RunId]:
        """Deletes whole run files (by file mtime) past the retention
        window, and drops their entries from the index. Returns the
        ``run_id``s that were purged. Index entries for other, still-
        live runs are preserved by rewriting the index file without
        the purged runs' entries. ``days`` must be finite and positive.
        """
        if not math.isfinite(days) or days <= 0:
            raise ValueError("retention days must be a finite, positive number")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        purged: list[RunId] = []
        for path in self._root.glob("*.jsonl"):
            if path.name == self._index_path.name:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                purged.append(RunId(path.stem))
                path.unlink()

        if purged:
            remaining = [
                entry for entry in self._read_index() if entry["run_id"] not in purged
            ]
            self._index_path.write_text(
                "".join(json.dumps(entry) + "\n" for entry in remaining), encoding="utf-8"
            )
        return purged
