"""Upper N-instance driver over the existing runner/graph/checkpointer.

One driver holds the shared ``RuntimeDeps`` and owns the checkpointer
lifecycle (``open``/``close``); the N ``AgentLoopRunner`` instances only
borrow it. Each instance keeps its own ``RuntimeState`` (distinct
``instance_id``), LangGraph thread (``run:instance:<id>``) and work leases,
so one instance's long job never blocks another's next model turn.
``drive`` surfaces per-instance results AS READY (first-completed order,
no batch barrier between instances).

What this driver never does: new strategy loop (reuses ``runner``/``graph``
model/tools/settle), tool-name branches, role reassignment (each runtime
keeps its ``current_agent``; the task claim in ``observe`` enforces role
eligibility), or mission/global close (instances never close the shared
binding; the coordinator decides from canonical mission state).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from redteam.orchestration.execution_collection import ExecutionOriginalCollection
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

from redteam.core.enums import AgentPhase
from redteam.core.ids import AgentInstanceId, MissionId, RunId, new_id
from redteam.orchestration.checkpointing import build_checkpointer, open_checkpointer
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import (
    CoordinationError,
    StateConflict,
    content_hash,
)
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState

logger = logging.getLogger(__name__)


class InstanceEvent(BaseModel):
    """One instance's terminal result, yielded as soon as it is ready."""

    instance_id: AgentInstanceId
    kind: Literal["completed", "failed", "terminated"]
    runtime: RuntimeState
    error: str | None = None


#: One owned actor: (instance, runner, base runtime, coroutine factory).
#: Factories (not coroutines) cross the validation boundary so a rejected
#: cohort never strands an unawaited coroutine.
_DriveCoro = Callable[[RuntimeState], Coroutine[Any, Any, RuntimeState]]
_DriveBuilders = list[tuple[AgentInstanceId, AgentLoopRunner, RuntimeState, _DriveCoro]]


async def _resume_entry(
    base: RuntimeState,
    *,
    runner: AgentLoopRunner,
    reconciliation: ExecutionReconciliation | None,
) -> RuntimeState:
    """One resume coroutine: safe recovers original POLICY, ready fresh OBSERVE."""
    if reconciliation is None:
        return await runner.resume_recovered(base)
    return await runner.resume_reconciled(reconciliation.runtime)


class InstanceDriver:
    """Owns checkpointer lifecycle; lends it to per-instance runners."""

    def __init__(
        self,
        deps: RuntimeDeps,
        *,
        settings: OrchestrationSettings | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self._deps = deps
        self._settings = settings or deps.settings
        self._checkpointer = checkpointer
        self._borrowed = checkpointer is not None
        self._checkpointer_context: Any | None = None
        self._opened = False
        # Owned runners keyed by instance id (reuse, so stop_instance reaches
        # the actually-driving runner); active drive tasks (task -> record).
        self._runners: dict[str, AgentLoopRunner] = {}
        self._active: dict[
            asyncio.Task[RuntimeState], tuple[AgentInstanceId, AgentLoopRunner, RuntimeState]
        ] = {}
        self._closed = False
        self._closing_task: asyncio.Task[None] | None = None
        self._run_ownership_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._run_ownership: AbstractAsyncContextManager[None] | None = None
        self._owned_run_id: str | None = None
        self._background: set[asyncio.Task] = set()

    def track_background(self, task: asyncio.Task) -> None:
        """Keep Run ownership until application-owned reasoning also stops."""
        if not self._opened or self._closed or self._owned_run_id is None:
            raise RuntimeError("background_reasoning_requires_owned_open_driver")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def quiesce(self) -> None:
        """Bounded drain without releasing the owned SDK or original Run lock."""
        self.stop_active()
        for task in tuple(self._background):
            task.cancel()
        pending_tasks = [*self._active, *self._background]
        if pending_tasks:
            _, pending = await asyncio.wait(
                pending_tasks, timeout=self._settings.drain_timeout_seconds,
            )
            if pending:
                raise RuntimeError("central_drain_incomplete_run_ownership_retained")

    async def own_run(self, run_id: str) -> None:
        """Own one Run until all actors and the checkpointer are closed.

        Call before specialist planning or checkpoint reconciliation, not
        merely before tool execution. Same-driver reuse is idempotent; another
        driver/process is refused before it can write original Run state.
        """
        async with self._run_ownership_lock:
            if not self._opened or self._closed:
                raise RuntimeError("InstanceDriver is not open")
            if self._owned_run_id is not None:
                if self._owned_run_id != run_id:
                    raise CoordinationError("driver_run_ownership_mismatched")
                return
            context = self._deps.central_state.exclusive_run(run_id)
            await context.__aenter__()
            if self._closed:
                await context.__aexit__(None, None, None)
                raise RuntimeError("InstanceDriver is not open")
            self._run_ownership, self._owned_run_id = context, run_id

    async def open(self) -> None:
        """Take ownership of the shared checkpointer (idempotent)."""
        if self._closing_task is not None and not self._closing_task.done():
            raise RuntimeError("InstanceDriver is closing")
        if self._opened:
            return
        self._closing_task = None
        if self._borrowed:
            self._opened = True
            self._closed = False
            return
        if self._settings.checkpointing:
            if self._settings.checkpoint_backend == "memory":
                self._checkpointer = build_checkpointer(
                    enabled=self._settings.checkpointing,
                    registry=self._deps.state_codecs,
                )
            elif self._settings.checkpoint_backend == "sqlite":
                self._checkpointer_context = open_checkpointer(
                    registry=self._deps.state_codecs,
                    enabled=self._settings.checkpointing,
                    backend=self._settings.checkpoint_backend,
                    path=self._settings.checkpoint_path,
                )
                self._checkpointer = await self._checkpointer_context.__aenter__()
        self._opened = True
        self._closed = False

    async def close(self) -> None:
        """Stop active actors first, wait for them, then release the SDK.

        Safe to call twice. Teardown failures are never silent and never
        leak raw text: after ALL close work, a bounded ``RuntimeError``
        naming only ``<where>:<ExcType>`` pairs propagates. Logs carry
        class names only, never ``exc_info`` bodies.
        """
        # One driver-owned cleanup, even when several callers close or the
        # waiting caller is cancelled. Never cancel sibling actors merely by
        # cancelling this wait, or strand a live Run's ownership forever.
        if self._closing_task is None:
            self._closed = True
            self._closing_task = asyncio.create_task(self._close_owned())
        try:
            await asyncio.shield(self._closing_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._closing_task)
            raise

    async def _close_owned(self) -> None:
        """Drain and release inside the existing driver, not a new executor."""
        teardown: list[str] = []
        if self._background:
            await self._deps.central_state.transaction(lambda tx: tx.set_settlement_gate(True))
            pending_reasoning = list(self._background)
            await self.quiesce()
            await asyncio.gather(*pending_reasoning, return_exceptions=True)
        for _task, (instance_id, runner, _base) in list(self._active.items()):
            try:
                runner.request_stop()
            except Exception as exc:  # noqa: BLE001 - collected, raised below
                logger.warning(
                    "instance driver stop failed for %s: %s",
                    instance_id,
                    type(exc).__name__,
                )
                teardown.append(f"stop:{instance_id}:{type(exc).__name__}")
        if self._active:
            tasks = list(self._active.keys())
            _done, pending = await asyncio.wait(
                tasks, timeout=self._settings.drain_timeout_seconds,
            )
            if pending:
                from redteam.orchestration.central_runtime import abandon

                for task in pending:
                    _instance, runner, base = self._active[task]
                    await abandon(self._deps, runner.failure_runtime or base)
                # Keep the owning checkpoint and Run lock. Returning them
                # while actors can still write would permit unsafe replay.
                raise RuntimeError("central_actor_drain_incomplete_run_ownership_retained")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results, strict=True):
                record = self._active.pop(task, None)
                label = record[0] if record is not None else "?"
                if record is not None and self._deps.central_state is not None:
                    from redteam.orchestration.central_runtime import actor_returned

                    _instance, runner, base = record
                    original = (result if isinstance(result, RuntimeState)
                                else runner.failure_runtime or base)
                    await actor_returned(self._deps, original,
                                         failed=isinstance(result, BaseException))
                if isinstance(result, asyncio.CancelledError):
                    logger.debug("instance %s cancelled during close", label)
                elif isinstance(result, BaseException):
                    logger.warning(
                        "instance %s ended with %s during close",
                        label,
                        type(result).__name__,
                    )
                    teardown.append(f"actor:{label}:{type(result).__name__}")
        if self._checkpointer_context is not None:
            context = self._checkpointer_context
            self._checkpointer_context = None
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - collected, raised below
                logger.warning(
                    "instance driver checkpointer close failed: %s",
                    type(exc).__name__,
                )
                teardown.append(f"checkpointer:{type(exc).__name__}")
        if not self._borrowed:
            self._checkpointer = None
        self._opened = False
        async with self._recovery_lock:
            async with self._run_ownership_lock:
                if self._run_ownership is not None:
                    context = self._run_ownership
                    self._run_ownership = None
                    self._owned_run_id = None
                    try:
                        await context.__aexit__(None, None, None)
                    except Exception as exc:  # noqa: BLE001 - collected after actor drain
                        teardown.append(f"run_ownership:{type(exc).__name__}")
        if teardown:
            raise RuntimeError(f"instance driver close incomplete: {', '.join(teardown)}")

    async def __aenter__(self) -> InstanceDriver:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def runner_for(self, runtime: RuntimeState) -> AgentLoopRunner:
        """Return the owned runner for an instance, building it once.

        Reuse (not rebuild) is what lets ``stop_instance`` reach the
        actually-driving runner. One runner executes only one run instance;
        driving the same id under a different run raises loudly from the
        runner's own binding guard instead of silently forking state.
        """
        if not self._opened:
            raise RuntimeError("InstanceDriver is not open")
        key = str(runtime.instance_id)
        runner = self._runners.get(key)
        if runner is None:
            runner = AgentLoopRunner(
                self._deps, settings=self._settings, checkpointer=self._checkpointer
            )
            self._runners[key] = runner
        return runner

    def stop_instance(self, instance_id: AgentInstanceId | str) -> bool:
        """Ask one owned instance to stop after its in-flight work.

        Returns False when no runner exists for the id (nothing to stop).
        """
        runner = self._runners.get(str(instance_id))
        if runner is None:
            return False
        return runner.request_stop()

    def stop_active(self) -> int:
        """Cancel owned asyncio actors; this is not proof of external cancellation."""
        return sum(runner.request_stop() for runner in self._runners.values())

    async def recover_central_instance(self, runtime: RuntimeState, *, transfer_owner=False):
        """Explicit quiescent recovery with original checkpoint and owner CAS.

        The supplied ownership version is the caller's expected epoch. Racing
        transfers of that same original epoch cannot both succeed. A Run lock
        proves process loss; elapsed resource leases are never that proof.
        """
        from redteam.core.ports.control_plane import DurableControlRecovery
        from redteam.orchestration.central_state import check_evidence

        state = self._deps.central_state
        if state is None:
            raise StateConflict("central_recovery_requires_central_state")
        await self.own_run(str(runtime.run_id))
        async with self._recovery_lock:
            if self._active or any(runner.is_active for runner in self._runners.values()):
                raise StateConflict("central_recovery_requires_quiescent_run")
            runner = self.runner_for(runtime)
            snapshot_runtime = await runner.original_central_runtime(
                runtime.run_id, runtime.instance_id,
            )
            if (snapshot_runtime.work_owner != runtime.work_owner
                    or snapshot_runtime.task_ownership_version != runtime.task_ownership_version):
                raise StateConflict("stale_task_ownership")
            if any(getattr(snapshot_runtime, field) != getattr(runtime, field) for field in (
                "run_id", "engagement_id", "instance_id", "mission_id", "active_task_id",
                "current_agent", "started_at", "component_binding", "llm_request_policy",
            )):
                raise StateConflict("central_original_checkpoint_identity_mismatched")
            await state.request_ledger().recover_owner(
                str(runtime.run_id), owner=runtime.work_owner,
            )
            control = self._deps.control_plane
            if not isinstance(control, DurableControlRecovery):
                raise StateConflict("durable_control_recovery_not_bound")
            if runtime.started_at is None:
                raise StateConflict("central_original_run_clock_missing")
            await control.rehydrate_run(
                runtime.engagement_id, runtime.run_id, started_at=runtime.started_at,
            )
            _, outcome = await runner.recover_reconciling(
                runtime.run_id, instance_id=runtime.instance_id,
            )
            assert outcome is not None
            active_jobs = {}
            if not outcome.ready:
                if (not transfer_owner or outcome.errors
                        or self._deps.long_job_adapter is None):
                    return snapshot_runtime, outcome
                from redteam.core.contracts.execution import ExecutionState

                for identity in outcome.pending_invocations:
                    execution = await state.get_execution(identity)
                    if execution is None or not execution.job_ref:
                        return snapshot_runtime, outcome
                    try:
                        status = await asyncio.wait_for(
                            self._deps.long_job_adapter.status(execution.job_ref),
                            timeout=self._settings.tool_timeout_seconds,
                        )
                    except Exception as exc:
                        outcome.errors[identity] = type(exc).__name__
                        return snapshot_runtime, outcome
                    if (status.job_id != execution.job_ref or status.state not in {
                        ExecutionState.PENDING, ExecutionState.RUNNING,
                    }):
                        return snapshot_runtime, outcome
                    active_jobs[identity] = execution.job_ref
            recovered = outcome.runtime
            if transfer_owner:
                executions = await state.list_executions(task_id=str(runtime.active_task_id))
                checked = await check_evidence(
                    self._deps.evidence_store, run_id=state.run_id,
                    engagement_id=state.engagement_id,
                    evidence_ids=[str(item.proposal_evidence_id) for item in executions],
                )
                owner = new_id("recovered_owner")
                task = await state.transaction(lambda tx: tx.recover_actor(
                    str(runtime.active_task_id), previous_owner=runtime.work_owner,
                    version=runtime.task_ownership_version, instance_id=str(runtime.instance_id),
                    owner=owner, checked=checked, active_jobs=active_jobs,
                ))
                recovered = recovered.model_copy(update={
                    "task_owner": owner, "task_ownership_version": task.ownership_version,
                })
            if recovered != snapshot_runtime:
                # SQL + SDK/files are not one transaction. A crash here leaves
                # a binding mismatch and refuses resume, never a fresh actor.
                await runner.checkpoint_recovered_owner(snapshot_runtime, recovered)
            return recovered, outcome

    async def release_instance_claim(self, runtime: RuntimeState) -> None:
        """Explicit cleanup of an already-terminal canonical Task claim.

        This is NOT unfinished handoff, recovery, completion or permission to
        replay. A caller's terminal RuntimeState is insufficient authority.
        Unfinished Tasks remain held until a verified handoff contract exists.
        Work resource markers and request charges are never released here.
        """
        from redteam.core.enums import TaskStatus
        from redteam.orchestration.central_runtime import runtime_token

        await runtime_token(self._deps, runtime, admit=False)
        task = await self._deps.central_state.get_task(str(runtime.active_task_id))
        if task is None or task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            raise StateConflict("central_unfinished_task_requires_verified_recovery")
        # Terminal Tasks retain their original owner epoch for result
        # idempotency. The physical actor has already returned its slot.
        return

    def _validate_cohort(self, runtimes: list[RuntimeState]) -> None:
        if not runtimes:
            raise ValueError("drive requires at least one runtime")
        seen: set[str] = set()
        first = runtimes[0]
        for runtime in runtimes:
            if runtime.instance_id is None:
                raise ValueError("drive requires instance_id on every runtime")
            if (
                runtime.run_id != first.run_id
                or runtime.engagement_id != first.engagement_id
                or runtime.mission_id != first.mission_id
            ):
                raise ValueError(
                    "drive cohort must share one run/mission/engagement"
                )
            key = str(runtime.instance_id)
            if key in seen:
                raise ValueError(f"duplicate drive instance: {key}")
            seen.add(key)

    def drive(self, runtimes: list[RuntimeState], *, wake: asyncio.Event | None = None,
              on_wakeup: Callable[[], Awaitable[datetime | None]] | None = None,
              ) -> AsyncGenerator[InstanceEvent, None]:
        """Run N instances concurrently; yield each result as soon as ready.

        Fresh-start entry (``runner.run``). For resume, use ``drive_resumed``.
        Each event carries that instance's OWN runtime -- never a sibling's.
        Errors surface as the exception type name only, never raw text.
        Abandoning the iterator leaves other instances running under driver
        ownership (``stop_instance``/``close`` own teardown); only an
        abnormal exit stops the rest gracefully before propagating.
        """
        self._validate_cohort(runtimes)
        self._reject_driving_ids([runtime.instance_id for runtime in runtimes])
        builders: _DriveBuilders = []
        for runtime in runtimes:
            # Narrowed by _validate_cohort above; kept explicit for typing.
            assert runtime.instance_id is not None
            runner = self.runner_for(runtime)
            builders.append((runtime.instance_id, runner, runtime, runner.run))
        return self._drain_as_ready(builders, wake=wake, on_wakeup=on_wakeup)

    def drive_resumed(
        self, items: list[tuple[RuntimeState, ExecutionReconciliation | None]]
    ) -> AsyncGenerator[InstanceEvent, None]:
        """As-ready compose for RESUME under this driver's sole lifecycle.

        Justified (not a parallel loop): every task is registered in
        ``_active``, so ``stop_instance``/``close`` own the actually
        running actors -- the CLI must not shadow this with its own tasks.
        ``None`` reconciliations take the owned-runner ``resume_recovered``
        path (a safe APPROVAL_PENDING re-enters original POLICY, never a
        fresh drive); ready reconciliations enter a fresh OBSERVE via
        ``resume_reconciled``. Held reconciliations must never be passed.
        """
        return self._drain_as_ready(self._resume_builders(items))

    def _resume_builders(
        self, items: list[tuple[RuntimeState, ExecutionReconciliation | None]],
    ) -> _DriveBuilders:
        runtimes = [runtime for runtime, _ in items]
        self._validate_cohort(runtimes)
        self._reject_driving_ids([runtime.instance_id for runtime in runtimes])
        # Held/cohort validation runs BEFORE any coroutine is built, so a
        # later invalid entry cannot strand an unawaited coroutine.
        for _runtime, reconciliation in items:
            if reconciliation is not None and not reconciliation.ready:
                raise ValueError(
                    f"held reconciliation must not drive: {_runtime.instance_id}"
                )
        builders: _DriveBuilders = []
        for runtime, reconciliation in items:
            assert runtime.instance_id is not None
            runner = self.runner_for(runtime)
            builders.append((
                runtime.instance_id,
                runner,
                runtime,
                functools.partial(
                    _resume_entry, runner=runner, reconciliation=reconciliation
                ),
            ))
        return builders

    async def recover_unstarted_activations(
        self, run_id: str, *, checkpoint_instances: set[str], instance_ids: set[str] | None = None,
    ) -> list[RuntimeState]:
        """Prove zero-entry reservations without granting fresh identities/budget.

        The Run owner fences other native drivers. Outside store reads happen
        outside the coordinator transaction, followed by a canonical recheck.
        Missing entered/legacy records refuse recovery before any actor starts.
        """
        from redteam.orchestration.central_runtime import runtime_token

        await self.own_run(run_id)
        state = self._deps.central_state
        if self._active or any(runner.is_active for runner in self._runners.values()):
            raise StateConflict("central_recovery_requires_quiescent_run")
        assert self._settings.llm_request_policy is not None
        assert self._deps.engagement is not None
        records = await self._deps.evidence_store.list_for_run(RunId(run_id))
        inventory = await state.activation_inventory(instance_ids=instance_ids)
        mission_id, _ = await state.transaction(lambda tx: tx.binding(), write=False)
        mission = await state.get_mission(MissionId(mission_id))
        if mission is None:
            raise StateConflict("central_original_mission_missing")
        originals = []
        for proof in inventory:
            instance_id = proof["instance_id"]
            if instance_ids is not None and instance_id not in instance_ids:
                continue
            if instance_id in checkpoint_instances or proof["launch_stage"] != "reserved":
                continue
            task = await state.check_unentered(instance_id=instance_id)
            runtime = RuntimeState(
                run_id=RunId(run_id), engagement_id=mission.engagement_id,
                instance_id=AgentInstanceId(instance_id), mission_id=mission.mission_id,
                active_task_id=task.task_id, current_agent=task.assigned_agent,
                current_goal=mission.objective, started_at=mission.created_at,
                task_ownership_version=task.ownership_version,
                task_owner=task.occupant_owner,
                component_binding=self._deps.component_binding,
                llm_request_policy=self._settings.llm_request_policy.model_dump(mode="json"),
                remaining_budget=self._deps.engagement.execution_budget.max_actions,
            )
            if (await self.runner_for(runtime).checkpoint_exists(runtime)
                    or any(record.metadata.get("instance_id") == instance_id
                           for record in records)):
                raise StateConflict("activation_owner_activity_checkpoint_required")
            await runtime_token(self._deps, runtime)
            await state.check_unentered(instance_id=instance_id)
            originals.append(runtime)
        return originals

    async def reconcile_planning_writes(self, run_id: str) -> None:
        """Recover confirmed storage fences before original checkpoint reads.

        Each owning wrapper checks its proof; this driver only supplies
        the existing exclusive Run/quiescent lifecycle boundary. No Task claim,
        unknown request charge, or execution marker is released here.
        """
        await self.own_run(run_id)
        # Central graph/result writes already commit together. Individual
        # originals are inspected on recovery, never a global evidence fence.
        flush = getattr(self._deps.event_bus, "flush", None)
        if flush is not None:
            await flush()
        return

    async def collect_checkpointless_originals(
        self, run_id: str, *, checkpoint_instances: set[str],
    ) -> list[ExecutionOriginalCollection]:
        """Preserve proved originals, but keep entered/no-checkpoint actors held.

        Explicit collection entry, not an automatic fallback from resume.
        The existing Run owner/quiescent lock prevents concurrent actors and
        driver teardown. No Control, model, Graph, Work/Task settlement, claim
        release, accounting restoration or new checkpoint runs here.
        """
        from redteam.orchestration.central_recovery import collect_original
        from redteam.orchestration.central_state import check_evidence, check_execution_receipt
        from redteam.orchestration.execution_collection import (
            ExecutionOriginal,
            ExecutionOriginalCollection,
        )

        await self.own_run(run_id)
        if self._active or any(runner.is_active for runner in self._runners.values()):
            raise StateConflict("central_recovery_requires_quiescent_run")
        state = self._deps.central_state
        collected = []
        for proof in await state.activation_inventory():
            if (proof["instance_id"] in checkpoint_instances
                    or proof["launch_stage"] == "reserved"):
                continue
            task = await state.get_task(proof["task_id"])
            original_results = []
            for execution in await state.list_executions(task_id=proof["task_id"]):
                try:
                    checked = await check_evidence(
                        self._deps.evidence_store, run_id=state.run_id,
                        engagement_id=state.engagement_id,
                        evidence_ids=[str(execution.proposal_evidence_id)],
                    )
                    check_execution_receipt(
                        execution, checked[0].record, result=False, task=task,
                    )
                    original = await collect_original(self._deps, execution, checked[0].record)
                    original_results.append(ExecutionOriginal(
                        invocation_id=str(execution.invocation_id),
                        state="known" if original else "held",
                        reason_code="original_collected" if original else "original_unresolved",
                        original_receipt_id=str(original[1]) if original else None,
                    ))
                except Exception as exc:
                    original_results.append(ExecutionOriginal(
                        invocation_id=str(execution.invocation_id), state="held",
                        reason_code=type(exc).__name__,
                    ))
            collected.append(ExecutionOriginalCollection(
                run_id=run_id, instance_id=proof["instance_id"], originals=original_results,
            ))
        return collected

    async def drive_ready_tasks(
        self, base: RuntimeState, *, max_instances: int,
        resume_items: list[tuple[RuntimeState, ExecutionReconciliation | None]] | None = None,
        unstarted_items: list[RuntimeState] | None = None,
        selected_instances: set[str] | None = None,
        allow_new_tasks: bool = True,
        on_wakeup: Callable[[], Awaitable[datetime | None]] | None = None,
        wake_events: tuple[str, ...] = (),
        wake: asyncio.Event | None = None,
    ) -> AsyncGenerator[InstanceEvent, None]:
        """Opt-in ready-Task lifecycle on the SAME driver/runners/checkpointer.

        The coordinator claims before launch. New Tasks wake this iterator even
        while another instance is busy; a finished instance is never reset.
        No ready work means quiescence, NOT goal success. Approval/unknown
        instances keep their identities for ``drive_resumed`` reconciliation.
        Optional application wakeups return an observed UTC-aware deadline;
        the driver only waits/coalesces and never interprets their domain data.
        """
        from redteam.core.contracts.events import EventType

        if not self._opened:
            raise RuntimeError("InstanceDriver is not open")
        if self._deps.specialist_policy is not None:
            await self.own_run(str(base.run_id))
        # Resume keeps the original checkpoint/instance/claim. Validate the
        # complete selection before marking it running or making coroutines.
        builders = self._resume_builders(resume_items) if resume_items else []
        if unstarted_items:
            # These are SAME original actors, not newly activated Tasks. Recheck
            # before any builders start; caller lists are not authority.
            checked = await self.recover_unstarted_activations(
                str(base.run_id), checkpoint_instances={str(runtime.instance_id)
                    for runtime, _ in resume_items or []},
                instance_ids=selected_instances,
            )
            by_id = {runtime.instance_id: runtime for runtime in checked}
            for runtime in unstarted_items:
                if (runtime.instance_id not in by_id
                        or content_hash(runtime) != content_hash(by_id[runtime.instance_id])):
                    raise StateConflict("activation_runtime_proof_mismatch")
            self._validate_cohort([runtime for runtime, _ in resume_items or []] + unstarted_items)
            for runtime in unstarted_items:
                assert runtime.instance_id is not None
                runner = self.runner_for(runtime)
                builders.append((runtime.instance_id, runner, runtime, runner.run))
        wake = wake if wake is not None else asyncio.Event()
        listening = True

        async def changed(event):
            if (listening and event.run_id == base.run_id
                    and event.engagement_id == base.engagement_id):
                wake.set()

        # Notifications only hint at work. Canonical Tasks are checked on every
        # start/result, so lost or duplicate events cannot duplicate a launch.
        from redteam.orchestration.central_runtime import ACTOR_RETURNED

        for event_type in dict.fromkeys((EventType.TASK_CREATED, ACTOR_RETURNED, *wake_events)):
            await self._deps.event_bus.subscribe(event_type, changed)

        async def replenish() -> _DriveBuilders:
            if self._closed:
                return []
            from redteam.orchestration.central_runtime import ready_workers

            builders = await self._resource_builders(
                base, max_instances=max_instances, instance_ids=selected_instances,
            )
            if not allow_new_tasks:
                return builders
            try:
                runtimes = await ready_workers(
                    self._deps, base, max_instances=max_instances,
                )
            except StateConflict as exc:
                if str(exc) not in {"pending_operator_mutation",
                                    "goal_settlement_admission_closed"}:
                    raise
                return builders
            for runtime in runtimes:
                assert runtime.instance_id is not None
                runner = self.runner_for(runtime)
                builders.append((runtime.instance_id, runner, runtime, runner.run))
            return builders

        try:
            async for event in self._drain_as_ready(
                builders, replenish=replenish, wake=wake,
                on_wakeup=on_wakeup if allow_new_tasks else None,
            ):
                yield event
        finally:
            listening = False

    async def _resource_builders(
        self, base: RuntimeState, *, max_instances: int, instance_ids: set[str] | None,
    ) -> _DriveBuilders:
        """Recheck canonical waits on notifications/results, with the same SDK owner."""
        from redteam.core.enums import ApprovalStatus, MissionStatus, TaskStatus
        from redteam.core.ports.control_plane import DurableControlRecovery
        from redteam.orchestration.central_recovery import original_resource_keys
        from redteam.orchestration.central_runtime import tool_limit, worker_limit

        state = self._deps.central_state
        if state is None or base.mission_id is None:
            return []
        mission = await state.get_mission(base.mission_id)
        if mission is None or mission.status is not MissionStatus.ACTIVE:
            return []
        max_instances = min(max_instances, worker_limit(self._deps))
        builders: _DriveBuilders = []
        driving = {str(record[0]) for record in self._active.values()}
        for row in await state.activation_inventory(instance_ids=instance_ids):
            identity = row["instance_id"]
            if row["launch_stage"] != "returned" or identity in driving:
                continue
            task = await state.get_task(row["task_id"])
            if (task is None or task.status is not TaskStatus.BLOCKED
                    or not str(task.blocking_reason).startswith("resource:")):
                continue
            waits = await state.list_executions(task_id=row["task_id"])
            approved = True
            control = self._deps.control_plane
            for execution in waits:
                if execution.wait_reason == "resource" and execution.approval_ref:
                    if not isinstance(control, DurableControlRecovery):
                        approved = False
                        break
                    approval = await control.inspect_approval(
                        execution.approval_ref,
                    )
                    if approval is None or approval.status is not ApprovalStatus.APPROVED:
                        approved = False
                        break
            if not approved:
                continue
            runner = self.runner_for(base.model_copy(update={"instance_id": identity}))
            try:
                original = await runner.original_central_runtime(base.run_id, identity)
                if (original.work_owner != row["owner"]
                        or original.task_ownership_version != row["ownership_version"]
                        or str(original.active_task_id) != row["task_id"]):
                    raise CoordinationError("central_resource_checkpoint_owner_mismatch")
                comparisons = {str(e.invocation_id): await original_resource_keys(self._deps, e)
                               for e in waits if e.wait_reason == "resource"}
            except Exception as exc:  # Originals remain held with an explicit review diagnostic.
                def hold(tx, proof=row):
                    tx.hold_resource_actor(proof["task_id"], owner=proof["owner"],
                                           version=proof["ownership_version"])
                await state.transaction(hold)
                logger.warning("Resource continuation held: %s", type(exc).__name__)
                continue
            try:
                def reserve(tx, proof=row, instance=identity, keys=comparisons):
                    return tx.reserve_resource_actor(
                        proof["task_id"], owner=proof["owner"], version=proof["ownership_version"],
                        instance_id=instance, now=datetime.now(timezone.utc).timestamp(),
                        max_instances=max_instances, max_calls=tool_limit(self._deps),
                        per_target=self._settings.per_target_concurrency,
                        comparison_keys=keys,
                    )
                reserved = await state.transaction(reserve)
            except (CoordinationError, StateConflict) as exc:
                # Operator changes can close admission between the read and CAS.
                if str(exc) in {"mission_not_active", "goal_settlement_admission_closed",
                                "pending_operator_mutation"}:
                    break
                raise
            if reserved:
                assert original.instance_id is not None
                builders.append((original.instance_id, runner, original, runner.resume_resource))
        return builders

    def _reject_driving_ids(self, instance_ids: list[Any]) -> None:
        if self._recovery_lock.locked():
            raise StateConflict("driver_planning_write_recovery_in_progress")
        driving = {record[0] for record in self._active.values()}
        for instance_id in instance_ids:
            if str(instance_id) in driving:
                raise ValueError(f"instance already driving: {instance_id}")

    async def _drain_as_ready(
        self, builders: _DriveBuilders, *, replenish=None, wake: asyncio.Event | None = None,
        on_wakeup: Callable[[], Awaitable[datetime | None]] | None = None,
    ) -> AsyncGenerator[InstanceEvent, None]:
        """Yield owned task results first-completed-first; own the tasks.

        The driving reservation re-checks and the task creation below run
        back-to-back with no await between them: two iterators built while
        idle cannot both start the same instance, whichever is consumed
        second rejects.
        """
        from redteam.orchestration.central_runtime import worker_limit

        if len(builders) + len(self._active) > worker_limit(self._deps):
            raise StateConflict("central_worker_limit_reached")
        self._reject_driving_ids([instance_id for instance_id, _, _, _ in builders])
        if getattr(self._deps, "specialist_policy", None) is not None and builders:
            await self.own_run(str(builders[0][2].run_id))
            self._reject_driving_ids([instance_id for instance_id, _, _, _ in builders])
            if (self._deps.central_state is not None
                    and len(builders) + len(self._active) > worker_limit(self._deps)):
                raise StateConflict("central_worker_limit_reached")
        pending: dict[
            asyncio.Task[RuntimeState],
            tuple[AgentInstanceId, AgentLoopRunner, RuntimeState],
        ] = {}
        for instance_id, runner, base, make_coro in builders:
            task = asyncio.create_task(make_coro(base), name=f"instance:{instance_id}")
            pending[task] = (instance_id, runner, base)
            self._active[task] = (instance_id, runner, base)
        try:
            while True:
                if replenish is not None:
                    if wake is not None:
                        wake.clear()
                    more = await replenish()
                    self._reject_driving_ids([iid for iid, _, _, _ in more])
                    for iid, runner, base, make_coro in more:
                        task = asyncio.create_task(make_coro(base), name=f"instance:{iid}")
                        pending[task] = (iid, runner, base)
                        self._active[task] = (iid, runner, base)
                # The application owns the deadline and its planner task. This
                # callback only observes/signals; it must not await model I/O.
                # Central selection may wait for that owned planner while idle;
                # legacy selection returns as soon as its actors have returned.
                deadline = await on_wakeup() if on_wakeup and not self._closed else None
                if (deadline is None and pending and replenish is not None
                        and self._deps.central_state is not None):
                    deadline = datetime.now(timezone.utc) + timedelta(
                        seconds=self._settings.resource_wait_timeout_seconds,
                    )
                if not pending and deadline is None:
                    break
                if deadline is not None and deadline.tzinfo is None:
                    raise ValueError("driver_wakeup_timezone_required")
                if wake is not None and wake.is_set():
                    continue  # callback/event admitted work; re-read canonical Tasks
                timeout = (max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
                           if deadline is not None else None)
                waking = asyncio.create_task(wake.wait()) if wake is not None else None
                waiters = [*pending, *([waking] if waking is not None else [])]
                try:
                    done, _ = await asyncio.wait(
                        waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if waking is not None and not waking.done():
                        waking.cancel()
                        await asyncio.gather(waking, return_exceptions=True)
                if waking is not None:
                    done.discard(waking)
                for finished in [task for task in pending if task in done]:
                    instance_id, _runner, base = pending.pop(finished)
                    self._active.pop(finished, None)
                    try:
                        final = finished.result()
                    except asyncio.CancelledError:
                        result = InstanceEvent(
                            instance_id=instance_id,
                            kind="terminated",
                            runtime=_runner.failure_runtime or base,
                        )
                    except Exception as exc:  # noqa: BLE001 - type only, never raw text
                        result = InstanceEvent(
                            instance_id=instance_id,
                            kind="failed",
                            runtime=_runner.failure_runtime or base,
                            error=type(exc).__name__,
                        )
                    else:
                        result = InstanceEvent(
                            instance_id=instance_id,
                            kind="failed" if final.phase is AgentPhase.FAILED else "completed",
                            runtime=final,
                        )
                    from redteam.orchestration.central_runtime import actor_returned

                    await actor_returned(self._deps, result.runtime,
                                         failed=result.kind != "completed")
                    yield result
        except GeneratorExit:
            # Consumer went away: the rest keep running under driver
            # ownership; stop_instance()/close() own teardown. Never cancel
            # here -- that would kill independent instances for listening.
            raise
        except BaseException:  # noqa: BLE001 - graceful stop then propagate
            # Abnormal exit: ask the rest to stop gracefully, wait, then
            # propagate (gather errors chain onto the original, never hidden).
            for _task, (_iid, runner, _base) in list(pending.items()):
                try:
                    runner.request_stop()
                except Exception as stop_exc:  # noqa: BLE001 - teardown continues, logged
                    logger.warning(
                        "instance driver stop failed for %s: %s",
                        _iid,
                        type(stop_exc).__name__,
                    )
            if pending:
                await asyncio.gather(*pending)
            for task in list(pending):
                self._active.pop(task, None)
            raise
