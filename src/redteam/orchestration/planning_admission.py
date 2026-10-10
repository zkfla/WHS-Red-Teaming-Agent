"""Relevant-input CAS for new strategy producers, not a replacement global token.

The producer validates schema/references before calling admit. Its bounded apply
callback writes canonical Tasks and queues events using the same ChangeSet. No
model, filesystem, graph-store or network I/O is allowed in that callback.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.ids import MissionId, TaskId, new_id
from redteam.orchestration.coordination import ChangeSet, Coordinator, StateConflict, content_hash


@dataclass(frozen=True)
class PlanningInputStamp:
    run_id: str
    mission_id: str
    goal_hash: str
    generation: int
    strategy_revision: int
    input_binding_hash: str
    task_reads: tuple[tuple[str, str], ...]
    reference_reads: tuple[tuple[str, int], ...]
    valid_until: datetime | None = None


def goal_input_hash(tx: ChangeSet, mission_id: str) -> str:
    """One relevant goal projection for planning and reserved-launch recovery."""
    mission = tx.tasks.peek_mission(MissionId(mission_id))
    if mission is None:
        raise StateConflict("planning_mission_missing")
    return content_hash({
        "objective": mission.objective, "criteria": mission.success_criteria,
        "constraints": mission.operator_constraints, "priorities": mission.priorities,
        "preferences": mission.operator_preferences,
    })


async def task_input_hash(tx: ChangeSet, task_id: str) -> str:
    """Relevant Task inputs, excluding unrelated sibling/revision changes."""
    task = await tx.tasks.get_task(TaskId(task_id))
    if task is None:
        raise StateConflict("planning_task_missing")
    return content_hash({
        "contract": completion_contract_hash(task), "status": task.status,
        "dependencies": sorted(map(str, task.dependency_task_ids)),
        "completion_receipt": task.completion_evidence_id,
        "role": task.assigned_agent, "required": task.required,
        "required_capability": task.required_capability,
    })


class PlanningAdmission:
    def __init__(self, coordinator: Coordinator) -> None:
        self.coordinator = coordinator

    @staticmethod
    def reference_versions(tx: ChangeSet, run_id: str, references) -> tuple[tuple[str, int], ...]:
        reads = []
        for reference in sorted(set(references)):
            row = tx.records.get(("planning_reference", f"{run_id}:{reference}"), {})
            if row.get("writer"):
                raise StateConflict("planning_reference_update_pending")
            reads.append((reference, row.get("version", 0)))
        return tuple(reads)

    @classmethod
    def check_references(cls, tx: ChangeSet, run_id: str, reads) -> None:
        if cls.reference_versions(tx, run_id, [key for key, _ in reads]) != tuple(reads):
            raise StateConflict("planning_reference_changed")

    async def capture_references(self, run_id: str, references) -> tuple[tuple[str, int], ...]:
        async def capture(tx):
            return self.reference_versions(tx, run_id, references)
        return await self.coordinator.change(capture)

    async def bind_inputs(self, run_id: str, inputs: dict[str, Any]) -> None:
        """Assembler-owned scope/profile/tool binding; no model supplied values."""
        async def bind(tx: ChangeSet) -> None:
            digest = content_hash(inputs)
            key = ("planning_inputs", run_id)
            if key in tx.records and tx.records[key] != digest:
                raise StateConflict("planning_input_binding_changed_new_run_required")
            tx.records[key] = digest
        await self.coordinator.change(bind)

    async def capture(
        self, run_id: str, mission_id: str, *, task_ids: Iterable[str] = (),
        reference_keys: Iterable[str] = (),
        valid_until: datetime | None = None,
    ) -> PlanningInputStamp:
        """References include any negative-query set used by the producer."""
        task_ids, reference_keys = tuple(sorted(set(task_ids))), tuple(sorted(set(reference_keys)))

        async def capture(tx: ChangeSet) -> PlanningInputStamp:
            tx.admit(run_id, mission_id)
            binding = tx.records.get(("planning_inputs", run_id))
            if binding is None:
                raise StateConflict("planning_input_binding_missing")
            reads = []
            for identity in task_ids:
                task = await tx.tasks.get_task(TaskId(identity))
                if task is None or str(task.mission_id) != mission_id:
                    raise StateConflict("planning_task_scope_mismatch")
                reads.append((identity, await task_input_hash(tx, identity)))
            refs = self.reference_versions(tx, run_id, reference_keys)
            return PlanningInputStamp(
                run_id=run_id, mission_id=mission_id, goal_hash=goal_input_hash(tx, mission_id),
                generation=tx.generation(run_id, mission_id),
                strategy_revision=tx.records.get(("strategy_revision", run_id), 0),
                input_binding_hash=binding, task_reads=tuple(reads), reference_reads=tuple(refs),
                valid_until=valid_until,
            )
        return await self.coordinator.change(capture)

    async def admit(
        self, stamp: PlanningInputStamp, *, proposal_id: str, proposal: dict[str, Any],
        apply: Callable[[ChangeSet], Coroutine[Any, Any, dict[str, Any]]],
    ) -> dict[str, Any]:
        if not proposal_id.strip():
            raise ValueError("planning_proposal_id_required")
        digest = content_hash(proposal)

        async def admit(tx: ChangeSet) -> dict[str, Any]:
            tx.binding(stamp.run_id, stamp.mission_id)
            key = ("applied_strategy", f"{stamp.run_id}:{proposal_id}")
            saved = tx.records.get(key)
            if saved is not None:
                if saved["digest"] != digest or saved["mission_id"] != stamp.mission_id:
                    raise StateConflict("planning_proposal_identity_conflict")
                # Pure replay does not activate tasks or issue new events, even
                # after stop. A new plan must still pass live admission below.
                return saved["result"]
            await self.check_current(tx, stamp)
            result = await apply(tx)
            tx.records[("strategy_revision", stamp.run_id)] = stamp.strategy_revision + 1
            tx.records[key] = {
                "digest": digest, "mission_id": stamp.mission_id, "result": result,
            }
            return result
        return await self.coordinator.change(admit)

    async def check_current(
        self, tx: ChangeSet, stamp: PlanningInputStamp, *, check_strategy: bool = True,
    ) -> None:
        """Reuse input admission without adopting a new strategy or mutating Tasks.

        A code artifact uses the same goal/task/input gate, but is not a
        strategy writer: an unrelated strategy revision must not reject it.
        The choice is code-owned, never taken from a model proposal.
        """
        tx.admit(stamp.run_id, stamp.mission_id)
        if stamp.valid_until is not None and stamp.valid_until <= datetime.now(timezone.utc):
            raise StateConflict("stale_plan_evidence_expired")
        if (goal_input_hash(tx, stamp.mission_id) != stamp.goal_hash
                or tx.generation(stamp.run_id, stamp.mission_id) != stamp.generation
                or (check_strategy and tx.records.get(("strategy_revision", stamp.run_id), 0)
                    != stamp.strategy_revision)
                or tx.records.get(("planning_inputs", stamp.run_id)) != stamp.input_binding_hash):
            raise StateConflict("stale_plan")
        for task_id, digest_read in stamp.task_reads:
            if await task_input_hash(tx, task_id) != digest_read:
                raise StateConflict("stale_plan_task_changed")
        try:
            self.check_references(tx, stamp.run_id, stamp.reference_reads)
        except StateConflict:
            raise StateConflict("stale_plan_reference_changed") from None

    async def begin_reference_update(self, run_id: str, references: Iterable[str]) -> str:
        """Fence BEFORE an external mutable write; interrupted fences stay held."""
        references = tuple(sorted(set(references)))
        writer = new_id("planning_writer")

        async def begin(tx: ChangeSet) -> str:
            for reference in references:
                key = ("planning_reference", f"{run_id}:{reference}")
                row = tx.records.setdefault(key, {"version": 0, "writer": None})
                if row["writer"] is not None:
                    raise StateConflict("planning_reference_update_pending")
                row.update(version=row["version"] + 1, writer=writer)
            return writer
        return await self.coordinator.change(begin)

    async def finish_reference_update(
        self, run_id: str, references: Iterable[str], writer: str,
    ) -> None:
        references = tuple(sorted(set(references)))

        async def finish(tx: ChangeSet) -> None:
            for reference in references:
                row = tx.records.get(("planning_reference", f"{run_id}:{reference}"))
                if row is None or row["writer"] != writer:
                    raise StateConflict("planning_reference_writer_mismatch")
                row["writer"] = None
        await self.coordinator.change(finish)
