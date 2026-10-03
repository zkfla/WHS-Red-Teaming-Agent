"""Concurrency-correct in-memory PathStore for tests and local runs."""

from __future__ import annotations

import asyncio
from typing import cast

from redteam.orchestration.paths.models import (
    PATH_STATUSES,
    STEP_STATUSES,
    PathPlan,
    PathValidationError,
    PathVersionConflict,
    changed_steps_and_descendants,
    is_active_status,
    normalize_path_status,
    normalize_step_status,
    step_definition_equal,
    validate_path_structure,
)

_ACTIVE_STATUS = "active"


def _check_plan(path: PathPlan) -> str:
    """Validate statuses and DAG; return the normalized path status."""
    normalized_status = normalize_path_status(path.status)
    if normalized_status not in PATH_STATUSES:
        raise PathValidationError(f"unknown path status: {path.status!r}")
    for step in path.steps:
        if normalize_step_status(step.status) not in STEP_STATUSES:
            raise PathValidationError(f"unknown step status: {step.status!r} (step {step.step_id})")
    validate_path_structure(path.steps)
    return normalized_status


class InMemoryPathStore:
    """In-memory investigation-path store."""

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str, int], PathPlan] = {}
        self._progress: dict[tuple[str, str, int, str], dict] = {}
        self._active: dict[str, tuple[str, int]] = {}
        self._lock = asyncio.Lock()

    def _merged(self, snapshot: PathPlan) -> PathPlan:
        return self.merged_view(snapshot)

    def merged_view(self, snapshot: PathPlan) -> PathPlan:
        """Merge stored progress into a snapshot copy (lock-free staged read)."""
        merged = snapshot.model_copy(deep=True)
        for step in merged.steps:
            record = self._progress.get(
                (snapshot.run_id, snapshot.path_id, snapshot.version, step.step_id)
            )
            if record is not None:
                step.status = record["status"]
                step.outcome = record["outcome"]
                step.evidence_ids = list(record["evidence_ids"])
        return merged

    def peek_active_pointer(self, run_id: str) -> tuple[str, int] | None:
        """Lock-free staged read for the atomic backend (same boundary rule)."""
        return self._active.get(run_id)

    def peek_snapshot(self, run_id: str, path_id: str, version: int) -> PathPlan | None:
        """Lock-free staged snapshot read (same boundary rule)."""
        return self._snapshots.get((run_id, path_id, version))

    def _plan_progress(self, snapshot: PathPlan) -> tuple[list[str], dict]:
        """Compute (changed ids, progress records) without mutating state."""
        run_id = snapshot.run_id
        path_id = snapshot.path_id
        version = snapshot.version
        parent = None
        if snapshot.parent_version is not None:
            parent = self._snapshots.get((run_id, path_id, snapshot.parent_version))
        parent_steps = {step.step_id: step for step in parent.steps} if parent else {}
        new_steps = {step.step_id: step for step in snapshot.steps}
        if parent is not None:
            justified = set(snapshot.changed_step_ids or [])
            for step_id, older in parent_steps.items():
                carried = self._progress.get(
                    (run_id, path_id, cast(int, snapshot.parent_version), step_id)
                )
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
            record_key = (run_id, path_id, version, step.step_id)
            prev_step = parent_steps.get(step.step_id)
            if (
                parent is not None
                and prev_step is not None
                and step_definition_equal(prev_step, step)
                and step.step_id not in snapshot.changed_step_ids
            ):
                carried = self._progress.get(
                    (run_id, path_id, cast(int, snapshot.parent_version), step.step_id)
                )
                if carried is not None:
                    seeded[record_key] = {
                        "status": carried["status"],
                        "outcome": carried["outcome"],
                        "evidence_ids": list(carried["evidence_ids"]),
                    }
                else:
                    seeded[record_key] = {
                        "status": normalize_step_status(prev_step.status),
                        "outcome": prev_step.outcome,
                        "evidence_ids": list(prev_step.evidence_ids),
                    }
            elif parent is None and snapshot.parent_version is None:
                seeded[record_key] = {
                    "status": normalize_step_status(step.status),
                    "outcome": step.outcome,
                    "evidence_ids": list(step.evidence_ids),
                }
            else:
                changed.append(step.step_id)
                seeded[record_key] = {
                    "status": "pending",
                    "outcome": None,
                    "evidence_ids": [],
                }
        return changed, seeded

    async def get_active(self, run_id: str) -> PathPlan | None:
        """Return the active path version for ``run_id`` merged with progress."""
        async with self._lock:
            pointer = self._active.get(run_id)
            if pointer is None:
                return None
            snapshot = self._snapshots.get((run_id, pointer[0], pointer[1]))
            if snapshot is None:
                return None
            return self._merged(snapshot)

    async def list_paths(self, run_id: str) -> list[PathPlan]:
        """Return every stored version for ``run_id`` merged with progress."""
        async with self._lock:
            snapshots = sorted(
                (plan for key, plan in self._snapshots.items() if key[0] == run_id),
                key=lambda plan: (plan.path_id, plan.version),
            )
            return [self._merged(snapshot) for snapshot in snapshots]

    async def register(
        self,
        path: PathPlan,
        expected_version: int | None = None,
        *,
        expected_path_id: str | None = None,
    ) -> PathPlan:
        """Store one version snapshot, optionally moving the active pointer."""
        normalized_status = _check_plan(path)
        async with self._lock:
            key = (path.run_id, path.path_id, path.version)
            if key in self._snapshots:
                raise PathVersionConflict(f"path version exists: {key}")
            if is_active_status(path.status):
                pointer = self._active.get(path.run_id)
                current = pointer[1] if pointer is not None else None
                if expected_path_id is not None and (
                    pointer is None or pointer[0] != expected_path_id
                ):
                    raise PathVersionConflict("active path identity changed")
                if expected_version != current:
                    raise PathVersionConflict(
                        f"stale active version: expected {expected_version}, current {current}"
                    )
            snapshot = path.model_copy(deep=True)
            changed, seeded = self._plan_progress(snapshot)
            self._snapshots[key] = snapshot
            self._progress.update(seeded)
            seen = set(snapshot.changed_step_ids or [])
            snapshot.changed_step_ids = list(snapshot.changed_step_ids or []) + [
                step_id for step_id in changed if step_id not in seen
            ]
            if normalized_status == _ACTIVE_STATUS:
                self._active[snapshot.run_id] = (snapshot.path_id, snapshot.version)
            return self._merged(snapshot)

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
            snapshot = self._snapshots.get((run_id, path_id, version))
            if snapshot is None:
                return None
            if self._active.get(run_id) != (path_id, version):
                raise PathVersionConflict(f"not the active version: {(path_id, version)}")
            if step_id not in {step.step_id for step in snapshot.steps}:
                return None
            record_key = (run_id, path_id, version, step_id)
            record = self._progress.get(record_key)
            if record is None:
                record = {"status": "pending", "outcome": None, "evidence_ids": []}
                self._progress[record_key] = record
            record["status"] = normalized
            if outcome is not None:
                record["outcome"] = outcome
            for evidence_id in evidence_ids:
                if evidence_id not in record["evidence_ids"]:
                    record["evidence_ids"].append(evidence_id)
            return self._merged(snapshot)
