"""Durable SQLite PathStore with the same API and errors as the memory store."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from redteam.orchestration.paths.memory import _check_plan
from redteam.orchestration.paths.models import (
    STEP_STATUSES,
    PathPlan,
    PathValidationError,
    PathVersionConflict,
    changed_steps_and_descendants,
    is_active_status,
    normalize_step_status,
    step_definition_equal,
)

_ACTIVE_STATUS = "active"

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS path_snapshots (
    run_id TEXT NOT NULL,
    path_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, path_id, version)
);
CREATE TABLE IF NOT EXISTS path_progress (
    run_id TEXT NOT NULL,
    path_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    evidence_ids TEXT NOT NULL,
    PRIMARY KEY (run_id, path_id, version, step_id)
);
CREATE TABLE IF NOT EXISTS path_active (
    run_id TEXT PRIMARY KEY,
    path_id TEXT NOT NULL,
    version INTEGER NOT NULL
);
"""


def _parse_plan(payload: str) -> PathPlan:
    return PathPlan.model_validate_json(payload)


def _load_progress(db: sqlite3.Connection, run_id: str, path_id: str, version: int) -> dict:
    rows = db.execute(
        "SELECT step_id, status, outcome, evidence_ids FROM path_progress "
        "WHERE run_id=? AND path_id=? AND version=?",
        (run_id, path_id, version),
    ).fetchall()
    return {
        row[0]: {"status": row[1], "outcome": row[2], "evidence_ids": json.loads(row[3])}
        for row in rows
    }


def _merged_plan(snapshot: PathPlan, progress: dict) -> PathPlan:
    merged = snapshot.model_copy(deep=True)
    for step in merged.steps:
        record = progress.get(step.step_id)
        if record is not None:
            step.status = record["status"]
            step.outcome = record["outcome"]
            step.evidence_ids = list(record["evidence_ids"])
    return merged


class SQLitePathStore:
    """SQLite-backed investigation-path store in a single file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as db:
            db.executescript(_SCHEMA)
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    async def get_active(self, run_id: str) -> PathPlan | None:
        """Return the active path version for ``run_id`` merged with progress."""
        async with self._lock:
            db = self._connect()
            try:
                row = db.execute(
                    "SELECT path_id, version FROM path_active WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    return None
                payload = db.execute(
                    "SELECT payload FROM path_snapshots WHERE run_id=? AND path_id=? AND version=?",
                    (run_id, row[0], row[1]),
                ).fetchone()
                if payload is None:
                    return None
                snapshot = _parse_plan(payload[0])
                progress = _load_progress(db, run_id, row[0], row[1])
                return _merged_plan(snapshot, progress)
            finally:
                db.close()

    async def list_paths(self, run_id: str) -> list[PathPlan]:
        """Return every stored version for ``run_id`` merged with progress."""
        async with self._lock:
            db = self._connect()
            try:
                rows = db.execute(
                    "SELECT path_id, version, payload FROM path_snapshots "
                    "WHERE run_id=? ORDER BY path_id, version",
                    (run_id,),
                ).fetchall()
                plans = []
                for path_id, version, payload in rows:
                    snapshot = _parse_plan(payload)
                    progress = _load_progress(db, run_id, path_id, version)
                    plans.append(_merged_plan(snapshot, progress))
                return plans
            finally:
                db.close()

    async def register(
        self,
        path: PathPlan,
        expected_version: int | None = None,
        *,
        expected_path_id: str | None = None,
    ) -> PathPlan:
        """Store one version snapshot, optionally moving the active pointer."""
        normalized_status = _check_plan(path)
        snapshot = path.model_copy(deep=True)
        async with self._lock:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    exists = db.execute(
                        "SELECT 1 FROM path_snapshots WHERE run_id=? AND path_id=? AND version=?",
                        (snapshot.run_id, snapshot.path_id, snapshot.version),
                    ).fetchone()
                    if exists is not None:
                        version_key = (
                            snapshot.run_id,
                            snapshot.path_id,
                            snapshot.version,
                        )
                        raise PathVersionConflict(
                            f"path version exists: {version_key}"
                        )
                    if is_active_status(snapshot.status):
                        active = db.execute(
                            "SELECT version, path_id FROM path_active WHERE run_id=?",
                            (snapshot.run_id,),
                        ).fetchone()
                        current = active[0] if active is not None else None
                        if expected_path_id is not None and (
                            active is None or active[1] != expected_path_id
                        ):
                            raise PathVersionConflict("active path identity changed")
                        if expected_version != current:
                            raise PathVersionConflict(
                                f"stale active version: expected {expected_version}, "
                                f"current {current}"
                            )
                    parent = None
                    parent_progress: dict = {}
                    if snapshot.parent_version is not None:
                        parent_row = db.execute(
                            "SELECT payload FROM path_snapshots "
                            "WHERE run_id=? AND path_id=? AND version=?",
                            (snapshot.run_id, snapshot.path_id, snapshot.parent_version),
                        ).fetchone()
                        if parent_row is not None:
                            parent = _parse_plan(parent_row[0])
                            parent_progress = _load_progress(
                                db, snapshot.run_id, snapshot.path_id, snapshot.parent_version
                            )
                    parent_steps = {step.step_id: step for step in parent.steps} if parent else {}
                    if parent is not None:
                        justified = set(snapshot.changed_step_ids or [])
                        new_steps = {step.step_id: step for step in snapshot.steps}
                        for step_id, older in parent_steps.items():
                            carried = parent_progress.get(step_id)
                            effective = normalize_step_status(
                                carried["status"] if carried is not None else older.status
                            )
                            if effective != "completed":
                                continue
                            newer = new_steps.get(step_id)
                            if newer is None or not step_definition_equal(older, newer):
                                if step_id not in justified:
                                    raise PathValidationError(
                                        f"completed step reset without justification: {step_id} "
                                        "(add it to changed_step_ids)"
                                    )
                    if parent is not None:
                        snapshot.changed_step_ids = changed_steps_and_descendants(
                            parent.steps,
                            snapshot.steps,
                            snapshot.changed_step_ids,
                        )
                    changed: list[str] = []
                    seeded: dict = {}
                    for step in snapshot.steps:
                        prev_step = parent_steps.get(step.step_id)
                        if (
                            parent is not None
                            and prev_step is not None
                            and step_definition_equal(prev_step, step)
                            and step.step_id not in snapshot.changed_step_ids
                        ):
                            carried = parent_progress.get(step.step_id)
                            if carried is not None:
                                seeded[step.step_id] = {
                                    "status": carried["status"],
                                    "outcome": carried["outcome"],
                                    "evidence_ids": list(carried["evidence_ids"]),
                                }
                            else:
                                seeded[step.step_id] = {
                                    "status": normalize_step_status(prev_step.status),
                                    "outcome": prev_step.outcome,
                                    "evidence_ids": list(prev_step.evidence_ids),
                                }
                        elif parent is None and snapshot.parent_version is None:
                            seeded[step.step_id] = {
                                "status": normalize_step_status(step.status),
                                "outcome": step.outcome,
                                "evidence_ids": list(step.evidence_ids),
                            }
                        else:
                            changed.append(step.step_id)
                            seeded[step.step_id] = {
                                "status": "pending",
                                "outcome": None,
                                "evidence_ids": [],
                            }
                    seen_ids = set(snapshot.changed_step_ids or [])
                    snapshot.changed_step_ids = list(snapshot.changed_step_ids or []) + [
                        step_id for step_id in changed if step_id not in seen_ids
                    ]
                    db.execute(
                        "INSERT INTO path_snapshots "
                        "(run_id, path_id, version, parent_version, status, payload) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            snapshot.run_id,
                            snapshot.path_id,
                            snapshot.version,
                            snapshot.parent_version,
                            snapshot.status,
                            snapshot.model_dump_json(),
                        ),
                    )
                    for step_id, record in seeded.items():
                        db.execute(
                            "INSERT INTO path_progress "
                            "(run_id, path_id, version, step_id, status, outcome, evidence_ids) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (
                                snapshot.run_id,
                                snapshot.path_id,
                                snapshot.version,
                                step_id,
                                record["status"],
                                record["outcome"],
                                json.dumps(record["evidence_ids"]),
                            ),
                        )
                    if normalized_status == _ACTIVE_STATUS:
                        db.execute(
                            "INSERT OR REPLACE INTO path_active(run_id, path_id, version) "
                            "VALUES(?,?,?)",
                            (snapshot.run_id, snapshot.path_id, snapshot.version),
                        )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                return _merged_plan(snapshot, seeded)
            finally:
                db.close()

    async def update_step(
        self,
        run_id: str,
        path_id: str,
        version: int,
        step_id: str,
        status: str,
        outcome: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> PathPlan | None:
        """Update progress for one step of the active path version."""
        if evidence_ids is None:
            evidence_ids = []
        normalized = normalize_step_status(status)
        if normalized not in STEP_STATUSES:
            raise PathValidationError(f"unknown step status: {status!r}")
        async with self._lock:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    payload = db.execute(
                        "SELECT payload FROM path_snapshots "
                        "WHERE run_id=? AND path_id=? AND version=?",
                        (run_id, path_id, version),
                    ).fetchone()
                    if payload is None:
                        db.rollback()
                        return None
                    active = db.execute(
                        "SELECT path_id, version FROM path_active WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    if active is None or (active[0], active[1]) != (path_id, version):
                        raise PathVersionConflict(f"not the active version: {(path_id, version)}")
                    snapshot = _parse_plan(payload[0])
                    if step_id not in {step.step_id for step in snapshot.steps}:
                        db.rollback()
                        return None
                    row = db.execute(
                        "SELECT status, outcome, evidence_ids FROM path_progress "
                        "WHERE run_id=? AND path_id=? AND version=? AND step_id=?",
                        (run_id, path_id, version, step_id),
                    ).fetchone()
                    if row is None:
                        current_outcome, current_evidence = (
                            None,
                            [],
                        )
                    else:
                        current_outcome, current_evidence = (
                            row[1],
                            json.loads(row[2]),
                        )
                    merged_evidence = list(current_evidence)
                    for evidence_id in evidence_ids:
                        if evidence_id not in merged_evidence:
                            merged_evidence.append(evidence_id)
                    db.execute(
                        "INSERT OR REPLACE INTO path_progress "
                        "(run_id, path_id, version, step_id, status, outcome, evidence_ids) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            run_id,
                            path_id,
                            version,
                            step_id,
                            normalized,
                            outcome if outcome is not None else current_outcome,
                            json.dumps(merged_evidence),
                        ),
                    )
                    db.commit()
                    progress = _load_progress(db, run_id, path_id, version)
                    return _merged_plan(snapshot, progress)
                except BaseException:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    raise
            finally:
                db.close()
