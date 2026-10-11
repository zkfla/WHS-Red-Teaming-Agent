"""Relevant-input CAS for new strategy producers, not a replacement global token.

The producer validates schema/references before calling admit. Its bounded apply
callback writes canonical Tasks and queues events using the same ChangeSet. No
model, filesystem, graph-store or network I/O is allowed in that callback.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.ids import MissionId, TaskId, new_id
from redteam.orchestration.coordination import ChangeSet, Coordinator, StateConflict, content_hash


class ReferenceWriteProof(BaseModel):
    """Producer-owned expected write, not evidence of external acceptance."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReferenceUpdate(BaseModel):
    """Write intent in the SAME coordination commit as the reference fences."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    writer: str = Field(min_length=1)
    references: tuple[tuple[str, StrictInt], ...]
    proof: ReferenceWriteProof | None = None
    state: Literal["pending", "confirmed"] = "pending"

    @model_validator(mode="after")
    def validate_references(self) -> ReferenceUpdate:
        keys = [key for key, _ in self.references]
        if (not keys or keys != sorted(set(keys)) or any(not key for key in keys)
                or any(version < 1 for _, version in self.references)):
            raise ValueError("reference_update_inventory_invalid")
        return self


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

    async def begin_reference_update(
        self, run_id: str, references: Iterable[str], *, proof: ReferenceWriteProof | None = None,
    ) -> str:
        """Fence BEFORE I/O, with an optional producer-owned recovery proof.

        The intent contains IDs/hashes, never original evidence or a prompt.
        Merely saving it does not prove that the external write happened.
        """
        references = tuple(sorted(set(references)))
        writer = new_id("planning_writer")

        async def begin(tx: ChangeSet) -> str:
            reads = []
            for reference in references:
                key = ("planning_reference", f"{run_id}:{reference}")
                row = tx.records.setdefault(key, {"version": 0, "writer": None})
                if row["writer"] is not None:
                    raise StateConflict("planning_reference_update_pending")
                row.update(version=row["version"] + 1, writer=writer)
                reads.append((reference, row["version"]))
            intent = ReferenceUpdate(run_id=run_id, writer=writer, references=tuple(reads),
                                     proof=proof)
            tx.records[("planning_writer", f"{run_id}:{writer}")] = intent.model_dump(mode="json")
            return writer
        return await self.coordinator.change(begin)

    @staticmethod
    def _writer_references(tx: ChangeSet, run_id: str, writer: str) -> tuple[tuple[str, int], ...]:
        prefix = f"{run_id}:"
        references = []
        for (kind, identity), row in tx.records.items():
            if (kind != "planning_reference" or not identity.startswith(prefix)
                    or row.get("writer") != writer):
                continue
            key, version = identity[len(prefix):], row.get("version")
            if not key or type(version) is not int or version < 1:
                raise StateConflict("planning_reference_inventory_invalid")
            references.append((key, version))
        return tuple(sorted(references))

    async def pending_reference_updates(self, run_id: str) -> tuple[ReferenceUpdate, ...]:
        """Snapshot fenced writers; legacy writers have NO recovery proof."""
        async def read(tx: ChangeSet) -> tuple[ReferenceUpdate, ...]:
            prefix = f"{run_id}:"
            journals = {}
            for (kind, identity), saved in tx.records.items():
                if kind != "planning_writer" or not (
                    identity.startswith(prefix)
                    or (isinstance(saved, dict) and saved.get("run_id") == run_id)
                ):
                    continue
                intent = ReferenceUpdate.model_validate(saved)
                if intent.run_id != run_id or identity != f"{run_id}:{intent.writer}":
                    raise StateConflict("planning_reference_inventory_mismatch")
                journals[intent.writer] = intent
            writers = {row["writer"] for (kind, identity), row in tx.records.items()
                       if kind == "planning_reference" and identity.startswith(prefix)
                       and row.get("writer") is not None}
            # A pending intent with all reference rows missing/cleared is not
            # a successfully empty inventory. Check BOTH sides of the index.
            writers.update(writer for writer, intent in journals.items()
                           if intent.state == "pending")
            pending = []
            for writer in sorted(writers):
                refs = self._writer_references(tx, run_id, writer)
                # Legacy fences remain held. Do not fabricate an expected
                # external write from whatever happens to be in a store now.
                intent = journals.get(writer) or ReferenceUpdate(
                    run_id=run_id, writer=writer, references=refs,
                )
                if (intent.run_id != run_id or intent.writer != writer
                        or intent.state != "pending" or intent.references != refs):
                    raise StateConflict("planning_reference_inventory_mismatch")
                pending.append(intent)
            return tuple(pending)
        return await self.coordinator.change(read)

    async def finish_reference_update(
        self, run_id: str, references: Iterable[str], writer: str,
        *, expected: ReferenceUpdate | None = None,
    ) -> None:
        """Confirm only the original COMPLETE fence inventory in one CAS.

        Recovery supplies the snapshot whose external proof it checked.
        An old acknowledged finish is idempotent, never clearing a new writer.
        """
        references = tuple(sorted(set(references)))

        async def finish(tx: ChangeSet) -> None:
            key = ("planning_writer", f"{run_id}:{writer}")
            saved = tx.records.get(key)
            if saved is None:
                raise StateConflict("planning_reference_write_intent_missing")
            intent = ReferenceUpdate.model_validate(saved)
            if (intent.run_id != run_id or intent.writer != writer
                    or tuple(key for key, _ in intent.references) != references):
                raise StateConflict("planning_reference_inventory_mismatch")
            if expected is not None and intent != expected:
                raise StateConflict("planning_reference_write_intent_changed")
            if intent.state == "confirmed":
                return
            if self._writer_references(tx, run_id, writer) != intent.references:
                raise StateConflict("planning_reference_writer_mismatch")
            for reference, _ in intent.references:
                tx.records[("planning_reference", f"{run_id}:{reference}")]["writer"] = None
            tx.records[key] = intent.model_copy(update={"state": "confirmed"}).model_dump(
                mode="json",
            )
        await self.coordinator.change(finish)
