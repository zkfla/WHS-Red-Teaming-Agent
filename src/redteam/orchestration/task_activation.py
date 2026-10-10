"""Atomic ready-Task activation, reusing existing claims and driver runtimes.

Records are launch receipts, not a second task queue. An interrupted launch
keeps the claim/instance identity. Only an explicit versioned zero-entry proof
permits a start without a checkpoint; possible entry requires reconciliation,
never a TTL release or implicit redispatch. Callers supply lifecycle limits.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import AgentInstanceId, EngagementId, MissionId, TaskId, new_id
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.component_binding import RuntimeComponentBinding
from redteam.orchestration.coordination import (
    ChangeSet,
    CoordinationError,
    Coordinator,
    StateConflict,
    content_hash,
)
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.operator_intents.service import is_task_suppressed
from redteam.orchestration.planning_admission import goal_input_hash, task_input_hash
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.claim import claim_mission_task
from redteam.orchestration.work.models import work_owner_for


@dataclass(frozen=True)
class ActivationRecoveryIdentity:
    """Assembly hint from original zero-entry proofs, never a checkpoint."""
    engagement_id: EngagementId
    mission_id: MissionId
    component_binding: RuntimeComponentBinding
    llm_request_policy: LLMRequestPolicy


class ActivationDiagnostic(BaseModel):
    """Bounded identity/status projection; never the stored prompt/runtime."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    instance_id: str
    role_id: str
    engagement_id: str
    state: Literal["launched", "held", "returned"]
    launch_stage: Literal["reserved", "entered"] | None
    reason_code: str
    task_status: TaskStatus | None
    task_claim_matches: bool
    request_states: dict[str, int]


class ActivationDiagnostics(BaseModel):
    """One canonical read snapshot, explicitly not cross-store recovery."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    run_id: str
    observed_at: datetime
    replay_permitted: Literal[False] = False
    external_stores: Literal["not_inspected"] = "not_inspected"
    request_states: dict[str, int]
    activations: list[ActivationDiagnostic]


async def read_activation_identity(path: str, run_id: str) -> ActivationRecoveryIdentity | None:
    """Read only existing SQLite records; no registry/model/transport assembly."""
    from redteam.orchestration.state_backend import inspect_sqlite_state

    return await inspect_sqlite_state(
        path, lambda tx: TaskActivation.bootstrap_identity(tx, run_id),
    )


async def read_activation_diagnostics(path: str, run_id: str) -> ActivationDiagnostics | None:
    """No setup, assembly, checkpoint writer, model, transport or dispatch."""
    from redteam.orchestration.state_backend import inspect_sqlite_state

    return await inspect_sqlite_state(path, lambda tx: TaskActivation.diagnostics(tx, run_id))


class TaskActivation:
    def __init__(self, coordinator: Coordinator, agents: AgentRegistry) -> None:
        self.coordinator, self.agents = coordinator, agents

    async def source_task_id(self, runtime: RuntimeState) -> TaskId | None:
        """Recover the immutable launch subject after task.settle clears it.

        This is a context reference, NOT a reclaim or a new dispatch. Approval
        and interrupted instances keep exactly the same identity and owner.
        """
        if runtime.active_task_id is not None or runtime.instance_id is None:
            return runtime.active_task_id

        async def read(tx: ChangeSet) -> TaskId | None:
            tx.binding(str(runtime.run_id), str(runtime.mission_id))
            rows = [row for (kind, _), row in tx.records.items()
                    if kind == "task_activation" and row["run_id"] == str(runtime.run_id)
                    and row["instance_id"] == str(runtime.instance_id)]
            if not rows:
                return None  # the pre-activation planning runtime has no launch subject
            if len(rows) != 1:
                raise StateConflict("task_activation_identity_mismatch")
            return TaskId(rows[0]["task_id"])

        return await self.coordinator.change(read)

    async def ready(
        self, base: RuntimeState, *, max_instances: int, max_activations: int,
        eligible_roles: set[str] | None = None,
    ) -> list[RuntimeState]:
        if base.mission_id is None or min(max_instances, max_activations) < 1:
            raise ValueError("task_activation_requires_mission_and_positive_limits")
        now = datetime.now(timezone.utc)
        mission_id = base.mission_id

        async def claim(tx: ChangeSet) -> list[RuntimeState]:
            binding = tx.binding(str(base.run_id), str(mission_id), str(base.engagement_id))
            mission = tx.tasks.peek_mission(mission_id)
            if mission is None:
                raise StateConflict("task_activation_mission_missing")
            if (binding["closed"] or mission.status is not MissionStatus.ACTIVE
                    or tx.pending(str(base.run_id), str(mission_id))):
                return []
            tx.admit(str(base.run_id), str(mission_id))
            reason = RunRequestLedger(self.coordinator).launch_blocked(tx, str(base.run_id))
            if reason is not None:
                tx.records[("task_activation_denial", str(base.run_id))] = reason
                return []
            saved_policy = tx.records.get(("llm_request_policy", str(base.run_id)))
            if (saved_policy is not None and base.llm_request_policy is not None
                    and saved_policy != base.llm_request_policy):
                raise StateConflict("activation_request_policy_changed_new_run_required")
            selection = {"max_instances": max_instances, "max_activations": max_activations}
            key = ("task_activation_policy", str(base.run_id))
            if key in tx.records and tx.records[key] != selection:
                raise StateConflict("task_activation_policy_changed_new_run_required")
            tx.records[key] = selection
            clock_key = ("task_activation_started_at", str(base.run_id))
            if clock_key not in tx.records:
                tx.records[clock_key] = (base.started_at or now).isoformat()
            started_at = datetime.fromisoformat(tx.records[clock_key])
            rows = [row for (kind, _), row in tx.records.items()
                    if kind == "task_activation" and row["run_id"] == str(base.run_id)]
            slots = min(max_instances - sum(row["state"] == "launched" for row in rows),
                        max_activations - len(rows))
            if slots <= 0:
                return []
            tasks = await tx.tasks.list_tasks(mission_id=base.mission_id)
            by_id = {task.task_id: task for task in tasks}
            activated: list[RuntimeState] = []
            for task in sorted(tasks, key=lambda item: (-item.priority, str(item.task_id))):
                if len(activated) >= slots:
                    break
                key = ("task_activation", f"{base.run_id}:{task.task_id}")
                if key in tx.records or task.occupant_owner is not None:
                    continue
                if task.status not in {TaskStatus.PENDING, TaskStatus.READY}:
                    continue  # approvals, unknown executions and blocked tasks are not new launches
                if is_task_suppressed(task, mission):
                    continue
                role = task.assigned_agent
                if role is None:
                    continue
                if eligible_roles is not None and str(role) not in eligible_roles:
                    continue
                definition = self.agents.definition(role)
                if (definition is None or not definition.schedulable
                        or self.agents.get(role) is None):
                    continue
                if any(ref not in by_id or by_id[ref].status is not TaskStatus.COMPLETED
                       for ref in task.dependency_task_ids):
                    continue
                if task.status is TaskStatus.PENDING:
                    await tx.tasks.transition_task(task.task_id, TaskStatus.READY, now=now)
                instance_id = AgentInstanceId(new_id("instance"))
                await claim_mission_task(
                    tx, task.task_id, owner=work_owner_for(base.run_id, instance_id), now=now,
                )
                # Deliberately build a clean instance: never reset and reuse a
                # terminal/approval runtime. Run-wide policies and clock survive.
                runtime = RuntimeState(
                    run_id=base.run_id, engagement_id=base.engagement_id,
                    mission_id=base.mission_id, current_goal=mission.objective,
                    instance_id=instance_id, current_agent=role, active_task_id=task.task_id,
                    remaining_budget=base.remaining_budget, started_at=started_at,
                    component_binding=base.component_binding,
                    llm_request_policy=saved_policy or base.llm_request_policy,
                )
                tx.records[key] = {
                    "run_id": str(base.run_id), "task_id": str(task.task_id),
                    "instance_id": str(instance_id), "state": "launched",
                    "runtime": runtime.model_dump(mode="json"),
                    "completion_contract_hash": completion_contract_hash(task),
                    "activation_generation": tx.generation(str(base.run_id), str(mission_id)),
                    "role_id": str(role),
                    # Written with the claim, BEFORE runner entry. Only this
                    # versioned proof can recover without a checkpoint.
                    "launch_stage": "reserved", "launch_proof_version": 1,
                    "runtime_digest": content_hash(runtime),
                    "goal_input_hash": goal_input_hash(tx, str(mission_id)),
                    "input_binding_hash": tx.records.get(("planning_inputs", str(base.run_id))),
                    "task_input_hash": await task_input_hash(tx, str(task.task_id)),
                }
                activated.append(runtime)
            return activated

        return await self.coordinator.change(claim)

    @staticmethod
    def _rows(tx: ChangeSet, run_id: str) -> list[dict]:
        rows = []
        for (kind, identity), row in tx.records.items():
            if kind != "task_activation":
                continue
            indexed = identity.startswith(f"{run_id}:")
            if not indexed and (not isinstance(row, dict) or row.get("run_id") != run_id):
                continue
            if (not isinstance(row, dict) or row.get("run_id") != run_id
                    or identity != f"{run_id}:{row.get('task_id')}"):
                raise StateConflict("task_activation_identity_mismatch")
            rows.append(row)
        return rows

    @classmethod
    def has_pending(cls, tx: ChangeSet, run_id: str) -> bool:
        """Launch/held records are outstanding even without a visible checkpoint."""
        try:
            rows = cls._rows(tx, run_id)
        except StateConflict:
            return True  # corrupt identity blocks completion, never means an empty cohort
        return any(row.get("state") != "returned" for row in rows)

    @classmethod
    async def diagnostics(cls, tx: ChangeSet, run_id: str) -> ActivationDiagnostics | None:
        """Inspect all launch identities; never skip a broken peer as absent.

        The reserved checker is the same as recovery's canonical proof, but
        live components, checkpoints, Work and Evidence are NOT checked here.
        Entered records remain possible execution even with no ledger rows.
        """
        rows = cls._rows(tx, run_id)
        if not rows:
            return None
        if len({row["instance_id"] for row in rows}) != len(rows):
            raise StateConflict("task_activation_identity_mismatch")
        request_states = RunRequestLedger.request_states(tx, run_id)
        activations = []
        for row in rows:
            runtime = RuntimeState.model_validate(row["runtime"])
            if (runtime.mission_id is None or runtime.instance_id is None
                    or runtime.current_agent is None or str(runtime.run_id) != run_id
                    or row["instance_id"] != str(runtime.instance_id)
                    or row["task_id"] != str(runtime.active_task_id)
                    or row["role_id"] != str(runtime.current_agent)
                    or row.get("runtime_digest") is not None
                    and row["runtime_digest"] != content_hash(runtime)):
                raise StateConflict("activation_runtime_proof_mismatch")
            task = await tx.tasks.get_task(TaskId(row["task_id"]))
            claim_matches = bool(task is not None and task.mission_id == runtime.mission_id
                                 and task.occupant_owner == runtime.work_owner)
            reason = "original_checkpoint_and_receipts_required"
            if row.get("launch_stage") != "entered":
                try:
                    await cls._check_reserved_state(tx, row)
                except (CoordinationError, StateConflict) as exc:
                    reason = str(exc)  # code-owned structural proof failures only
                else:
                    reason = "reserved_proof_consistent"
            activations.append(ActivationDiagnostic(
                task_id=row["task_id"], instance_id=row["instance_id"], role_id=row["role_id"],
                engagement_id=str(runtime.engagement_id),
                state=row["state"], launch_stage=row.get("launch_stage"), reason_code=reason,
                task_status=task.status if task is not None else None,
                task_claim_matches=claim_matches,
                request_states=RunRequestLedger.request_states(
                    tx, run_id, owner=runtime.work_owner,
                ),
            ))
        return ActivationDiagnostics(
            run_id=run_id, observed_at=datetime.now(timezone.utc), request_states=request_states,
            activations=sorted(activations, key=lambda row: (row.task_id, row.instance_id)),
        )

    @staticmethod
    async def _check_reserved_state(tx: ChangeSet, row: dict) -> RuntimeState:
        """One proof rule for declaration-only bootstrap and owned recovery."""
        if (row.get("launch_proof_version") != 1 or row.get("launch_stage") != "reserved"
                or row["state"] != "launched"):
            raise StateConflict("activation_checkpoint_required")
        runtime = RuntimeState.model_validate(row["runtime"])
        run, mission = str(runtime.run_id), str(runtime.mission_id)
        if (runtime.mission_id is None or runtime.instance_id is None
                or runtime.current_agent is None
                or row["run_id"] != run or row["instance_id"] != str(runtime.instance_id)
                or row["task_id"] != str(runtime.active_task_id)
                or row["role_id"] != str(runtime.current_agent)
                or row.get("runtime_digest") != content_hash(runtime)):
            raise StateConflict("activation_runtime_proof_mismatch")
        tx.binding(run, mission, str(runtime.engagement_id))
        tx.admit(run, mission)
        if (row["activation_generation"] != tx.generation(run, mission)
                or row.get("goal_input_hash") != goal_input_hash(tx, mission)
                or row.get("input_binding_hash") != tx.records.get(("planning_inputs", run))):
            raise StateConflict("activation_inputs_changed")
        task = await tx.tasks.get_task(TaskId(row["task_id"]))
        if (task is None or task.mission_id != runtime.mission_id
                or task.status is not TaskStatus.RUNNING
                or task.occupant_owner != runtime.work_owner
                or task.assigned_agent != runtime.current_agent
                or completion_contract_hash(task) != row["completion_contract_hash"]):
            raise StateConflict("activation_task_proof_changed")
        if row.get("task_input_hash") != await task_input_hash(tx, row["task_id"]):
            raise StateConflict("activation_task_proof_changed")
        for dependency_id in task.dependency_task_ids:
            dependency = await tx.tasks.get_task(dependency_id)
            if dependency is None or dependency.status is not TaskStatus.COMPLETED:
                raise StateConflict("activation_dependency_changed")
        if RunRequestLedger.owner_has_activity(tx, run, runtime.work_owner):
            raise StateConflict("activation_owner_activity_checkpoint_required")
        saved_policy = tx.records.get(("llm_request_policy", run))
        if saved_policy is not None and saved_policy != runtime.llm_request_policy:
            raise StateConflict("activation_request_policy_changed_new_run_required")
        return runtime

    async def _check_reserved(self, tx: ChangeSet, row: dict) -> RuntimeState:
        runtime = await self._check_reserved_state(tx, row)
        assert runtime.current_agent is not None  # checked by the shared structural proof
        definition = self.agents.definition(runtime.current_agent)
        if (definition is None or not definition.schedulable
                or self.agents.get(runtime.current_agent) is None):
            raise StateConflict("activation_task_proof_changed")
        return runtime

    @classmethod
    async def bootstrap_identity(
        cls, tx: ChangeSet, run_id: str,
    ) -> ActivationRecoveryIdentity | None:
        """Coherent ALL-reserved cohort, before live role/catalog validation.

        An entered/legacy/mixed or incomplete cohort cannot be reduced to its
        first valid actor. The owned driver must still verify real checkpoint,
        Work/Evidence absence, current components/config and recheck state.
        """
        rows = cls._rows(tx, run_id)
        if not rows:
            return None
        if len({row["instance_id"] for row in rows}) != len(rows):
            raise StateConflict("task_activation_identity_mismatch")
        identities = []
        for row in rows:
            runtime = await cls._check_reserved_state(tx, row)
            if (runtime.component_binding is None or runtime.llm_request_policy is None
                    or row.get("input_binding_hash") is None):
                raise StateConflict("activation_bootstrap_binding_missing")
            identities.append(ActivationRecoveryIdentity(
                runtime.engagement_id, runtime.mission_id,  # type: ignore[arg-type]
                runtime.component_binding,
                LLMRequestPolicy.model_validate(runtime.llm_request_policy),
            ))
        if any(identity != identities[0] for identity in identities[1:]):
            raise StateConflict("activation_bootstrap_cohort_mismatch")
        return identities[0]

    async def unstarted(
        self, run_id: str, *, checkpoint_instances: set[str],
        input_binding_hash: str | None = None,
    ) -> list[RuntimeState]:
        """Complete missing-checkpoint inventory or refuse the whole cohort.

        Legacy/entered/held rows have no zero-use proof; their absence is not
        evidence that they never executed. All rows are checked before returning.
        The caller must also prove absence of checkpoint, Work and receipts.
        """
        async def read(tx: ChangeSet) -> list[RuntimeState]:
            rows = self._rows(tx, run_id)
            if len({row["instance_id"] for row in rows}) != len(rows):
                raise StateConflict("task_activation_identity_mismatch")
            if input_binding_hash is not None and rows and tx.records.get(
                ("planning_inputs", run_id)
            ) != input_binding_hash:
                raise StateConflict("activation_configuration_changed_new_run_required")
            return [await self._check_reserved(tx, row) for row in rows
                    if row["instance_id"] not in checkpoint_instances]

        return await self.coordinator.change(read)

    async def entering(self, runtime: RuntimeState) -> None:
        """Persist possible-entry BEFORE graph/model/tool entry, including errors.

        Existing checkpoint resumes may already be entered. This marker is not
        resume permission: binding/checkpoint/receipts/Control remain authoritative.
        Generic dev runtimes without a specialist launch record are unchanged.
        """
        async def enter(tx: ChangeSet) -> None:
            matches = [row for row in self._rows(tx, str(runtime.run_id))
                       if row["instance_id"] == str(runtime.instance_id)]
            if not matches:
                return
            if len(matches) != 1:
                raise StateConflict("task_activation_identity_mismatch")
            row = matches[0]
            if row.get("launch_stage") == "reserved":
                original = await self._check_reserved(tx, row)
                if content_hash(runtime) != content_hash(original):
                    raise StateConflict("activation_runtime_proof_mismatch")
            row["launch_stage"] = "entered"

        await self.coordinator.change(enter)

    async def returned(self, runtime: RuntimeState, *, failed: bool = False) -> None:
        async def settle(tx: ChangeSet) -> None:
            rows = [row for (kind, _), row in tx.records.items()
                    if kind == "task_activation" and row["run_id"] == str(runtime.run_id)
                    and row["instance_id"] == str(runtime.instance_id)]
            if len(rows) != 1:
                raise StateConflict("task_activation_identity_mismatch")
            # Keep the canonical task claim, including on cancellation/error.
            # Only the existing explicit reconciliation path can hand it off.
            rows[0].update(state="held" if failed or runtime.pending_approval_id
                           or runtime.termination_reason == TerminationReason.RESOURCE_WAIT
                           else "returned",
                           termination_reason=runtime.termination_reason)
        await self.coordinator.change(settle)

    async def resuming(self, runtimes: list[RuntimeState]) -> None:
        """Account reconciled existing actors before ready-work replenishment.

        No new identity, claim, activation allowance or model budget is created.
        Receipt safety and checkpoint binding remain the runner's responsibility.
        All identities are validated before updating any row (also in memory).
        """
        async def mark(tx: ChangeSet) -> None:
            rows = []
            for runtime in runtimes:
                if runtime.mission_id is None:
                    raise StateConflict("task_activation_mission_missing")
                tx.binding(str(runtime.run_id), str(runtime.mission_id))
                matches = [row for (kind, _), row in tx.records.items()
                           if kind == "task_activation" and row["run_id"] == str(runtime.run_id)
                           and row["instance_id"] == str(runtime.instance_id)]
                if len(matches) != 1:
                    raise StateConflict("task_activation_identity_mismatch")
                row = matches[0]
                if (runtime.active_task_id is not None
                        and row["task_id"] != str(runtime.active_task_id)):
                    raise StateConflict("task_activation_subject_mismatch")
                if row["role_id"] != str(runtime.current_agent):
                    raise StateConflict("task_activation_role_mismatch")
                mission = tx.tasks.peek_mission(runtime.mission_id)
                if mission is None:
                    raise StateConflict("task_activation_mission_missing")
                if mission.status is not MissionStatus.ACTIVE:
                    continue  # a reconciler may project pause/stop, not launch it
                task = await tx.tasks.get_task(TaskId(row["task_id"]))
                if (task is None
                        or completion_contract_hash(task) != row["completion_contract_hash"]):
                    raise StateConflict("task_activation_contract_changed")
                if (runtime.active_task_id is not None
                        and task.occupant_owner != runtime.work_owner):
                    raise StateConflict("task_activation_claim_changed")
                rows.append(row)
            for row in rows:
                row["state"] = "launched"

        await self.coordinator.change(mark)
