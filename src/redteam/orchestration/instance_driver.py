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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from redteam.orchestration.execution_collection import ExecutionOriginalCollection
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

from redteam.core.enums import AgentPhase
from redteam.core.ids import AgentInstanceId, RunId
from redteam.orchestration.checkpointing import build_checkpointer, open_checkpointer
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import (
    CoordinationError,
    StateConflict,
    content_hash,
    coordinator_for,
)
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph.claim import release_terminal_task_claim
from redteam.orchestration.work.models import work_owner_for

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
            coordinator = self._deps.coordinator or coordinator_for(
                self._deps.task_graph_store, self._deps.path_store,
            )
            context = coordinator.exclusive_run(run_id)
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
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results, strict=True):
                record = self._active.pop(task, None)
                label = record[0] if record is not None else "?"
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

    async def release_instance_claim(self, runtime: RuntimeState) -> None:
        """Explicit cleanup of an already-terminal canonical Task claim.

        This is NOT unfinished handoff, recovery, completion or permission to
        replay. A caller's terminal RuntimeState is insufficient authority.
        Unfinished Tasks remain held until a verified handoff contract exists.
        Work resource markers and request charges are never released here.
        """
        if runtime.mission_id is None or runtime.active_task_id is None:
            return
        if runtime.instance_id is None:
            raise StateConflict("task_claim_instance_identity_required")
        # Copy identity before awaiting. Caller-owned mutable counters/phase
        # are never evidence of settlement or absence of external effects.
        run_id, mission_id, engagement_id, task_id, instance_id = (
            runtime.run_id, runtime.mission_id, runtime.engagement_id,
            runtime.active_task_id, runtime.instance_id,
        )
        await self.own_run(str(run_id))
        async with self._recovery_lock:
            if self._closed:
                raise RuntimeError("InstanceDriver is not open")
            if self._active:
                raise StateConflict("task_claim_release_requires_quiescent_driver")
            coordinator = self._deps.coordinator or coordinator_for(
                self._deps.task_graph_store, self._deps.path_store,
            )

            async def release(change: Any) -> None:
                change.binding(str(run_id), str(mission_id), str(engagement_id))
                mission = await change.tasks.get_mission(mission_id)
                if mission is None or mission.engagement_id != engagement_id:
                    raise StateConflict("task_claim_identity_mismatched")
                await release_terminal_task_claim(
                    change, task_id, mission_id=mission_id,
                    owner=work_owner_for(run_id, instance_id), now=datetime.now(timezone.utc),
                )

            await coordinator.change(release)

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

    def drive(self, runtimes: list[RuntimeState]) -> AsyncGenerator[InstanceEvent, None]:
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
        return self._drain_as_ready(builders)

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
        self, run_id: str, *, checkpoint_instances: set[str],
    ) -> list[RuntimeState]:
        """Prove zero-entry reservations without granting fresh identities/budget.

        The Run owner fences other native drivers. Outside store reads happen
        outside the coordinator transaction, followed by a canonical recheck.
        Missing entered/legacy records refuse recovery before any actor starts.
        """
        from redteam.orchestration.specialist_planner import specialist_planning_inputs
        from redteam.orchestration.task_activation import TaskActivation

        await self.own_run(run_id)
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store,
        )
        activation = TaskActivation(coordinator, self._deps.agents)
        binding_hash = content_hash(specialist_planning_inputs(self._deps))
        candidates = await activation.unstarted(
            run_id, checkpoint_instances=checkpoint_instances, input_binding_hash=binding_hash,
        )
        if not candidates:
            return []
        expected_policy = (self._settings.llm_request_policy.model_dump(mode="json")
                           if self._settings.llm_request_policy else None)
        work = await self._deps.work_store.list_items(run_id=RunId(run_id))
        evidence = await self._deps.evidence_store.list_for_run(RunId(run_id))
        for runtime in candidates:
            if (runtime.component_binding != self._deps.component_binding
                    or runtime.llm_request_policy != expected_policy):
                raise StateConflict("activation_configuration_changed_new_run_required")
            if await self.runner_for(runtime).checkpoint_exists(runtime):
                raise StateConflict("activation_checkpoint_inventory_changed")
            if (any(item.instance_id == runtime.instance_id for item in work)
                    or any(str(item.metadata.get("instance_id")) == str(runtime.instance_id)
                           for item in evidence)):
                raise StateConflict("activation_owner_activity_checkpoint_required")
        checked = await activation.unstarted(
            run_id, checkpoint_instances=checkpoint_instances, input_binding_hash=binding_hash,
        )
        if [content_hash(runtime) for runtime in checked] != [
            content_hash(runtime) for runtime in candidates
        ]:
            raise StateConflict("activation_recovery_inventory_changed")
        return checked

    async def reconcile_planning_writes(self, run_id: str) -> None:
        """Recover confirmed storage fences before original checkpoint reads.

        Each owning wrapper checks its proof; this driver only supplies
        the existing exclusive Run/quiescent lifecycle boundary. No Task claim,
        unknown request charge, or execution marker is released here.
        """
        await self.own_run(run_id)
        async with self._recovery_lock:
            if self._closed:
                raise RuntimeError("InstanceDriver is not open")
            if self._active:
                raise StateConflict("planning_write_recovery_requires_quiescent_driver")
            held = False
            for store in (self._deps.evidence_store, self._deps.graph_store):
                reconcile = getattr(store, "reconcile_pending_writes", None)
                if callable(reconcile):
                    outcomes = await reconcile(run_id, supported_only=True)
                    held = held or any(outcome.state == "held" for outcome in outcomes)
            from redteam.orchestration.planning_admission import PlanningAdmission

            coordinator = self._deps.coordinator or coordinator_for(
                self._deps.task_graph_store, self._deps.path_store,
            )
            pending = await PlanningAdmission(coordinator).pending_reference_updates(run_id)
            if held or pending:
                raise StateConflict("planning_write_recovery_held")

    async def collect_checkpointless_originals(
        self, run_id: str, *, checkpoint_instances: set[str],
    ) -> list[ExecutionOriginalCollection]:
        """Preserve proved originals, but keep entered/no-checkpoint actors held.

        Explicit collection entry, not an automatic fallback from resume.
        The existing Run owner/quiescent lock prevents concurrent actors and
        driver teardown. No Control, model, Graph, Work/Task settlement, claim
        release, accounting restoration or new checkpoint runs here.
        """
        from redteam.orchestration.execution_collection import collect_execution_originals
        from redteam.orchestration.specialist_planner import specialist_planning_inputs
        from redteam.orchestration.task_activation import TaskActivation

        await self.own_run(run_id)
        async with self._recovery_lock:
            if self._closed:
                raise RuntimeError("InstanceDriver is not open")
            if self._active:
                raise StateConflict("execution_collection_requires_quiescent_driver")
            coordinator = self._deps.coordinator or coordinator_for(
                self._deps.task_graph_store, self._deps.path_store,
            )
            activation = TaskActivation(coordinator, self._deps.agents)
            originals = await activation.checkpointless_entered(
                run_id, checkpoint_instances=checkpoint_instances,
            )
            binding_hash = content_hash(specialist_planning_inputs(self._deps))
            expected_policy = (self._settings.llm_request_policy.model_dump(mode="json")
                               if self._settings.llm_request_policy else None)
            async def check_inputs(tx: Any) -> None:
                if originals and tx.records.get(("planning_inputs", run_id)) != binding_hash:
                    raise StateConflict("activation_configuration_changed_new_run_required")

            from redteam.orchestration.planning_admission import PlanningAdmission

            if await PlanningAdmission(coordinator).pending_reference_updates(run_id):
                raise StateConflict("planning_write_recovery_held")
            await coordinator.change(check_inputs)
            # Validate every actor before provider reads for any actor.
            for runtime in originals:
                if (runtime.component_binding != self._deps.component_binding
                        or runtime.llm_request_policy != expected_policy):
                    raise StateConflict("activation_configuration_changed_new_run_required")
                if await self.runner_for(runtime).checkpoint_exists(runtime):
                    raise StateConflict("activation_checkpoint_inventory_changed")
            results = [await collect_execution_originals(
                self._deps, runtime, original_task_id=str(runtime.active_task_id),
            ) for runtime in originals]
            after = await activation.checkpointless_entered(
                run_id, checkpoint_instances=checkpoint_instances,
            )
            await coordinator.change(check_inputs)
            if [content_hash(runtime) for runtime in after] != [
                content_hash(runtime) for runtime in originals
            ]:
                raise StateConflict("activation_collection_inventory_changed")
            return results

    async def drive_ready_tasks(
        self, base: RuntimeState, *, max_instances: int, max_activations: int,
        resume_items: list[tuple[RuntimeState, ExecutionReconciliation | None]] | None = None,
        unstarted_items: list[RuntimeState] | None = None,
        allow_new_tasks: bool = True,
        on_wakeup: Callable[[], Awaitable[datetime | None]] | None = None,
        wake_events: tuple[str, ...] = (),
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
        from redteam.orchestration.task_activation import TaskActivation

        if not self._opened:
            raise RuntimeError("InstanceDriver is not open")
        if self._deps.specialist_policy is not None:
            await self.own_run(str(base.run_id))
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store,
        )
        activation = TaskActivation(coordinator, self._deps.agents)
        # Resume keeps the original checkpoint/instance/claim. Validate the
        # complete selection before marking it running or making coroutines.
        builders = self._resume_builders(resume_items) if resume_items else []
        if unstarted_items:
            # These are SAME original actors, not newly activated Tasks. Recheck
            # before any builders start; caller lists are not authority.
            checked = await self.recover_unstarted_activations(
                str(base.run_id), checkpoint_instances={str(runtime.instance_id)
                    for runtime, _ in resume_items or []},
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
        if resume_items:
            await activation.resuming([runtime for runtime, _ in resume_items])
        wake = asyncio.Event()
        listening = True

        async def changed(event):
            if (listening and event.run_id == base.run_id
                    and event.engagement_id == base.engagement_id):
                wake.set()

        # Notifications only hint at work. Canonical Tasks are checked on every
        # start/result, so lost or duplicate events cannot duplicate a launch.
        for event_type in dict.fromkeys((EventType.TASK_CREATED, *wake_events)):
            await self._deps.event_bus.subscribe(event_type, changed)

        async def replenish() -> _DriveBuilders:
            if self._closed or not allow_new_tasks:
                return []
            from redteam.orchestration.agents.tool_access import ready_role_ids

            eligible = (await ready_role_ids(self._deps)
                        if self._deps.specialist_policy is not None else None)
            runtimes = await activation.ready(
                base, max_instances=max_instances, max_activations=max_activations,
                eligible_roles=eligible,
            )
            builders: _DriveBuilders = []
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
                await activation.returned(event.runtime, failed=event.kind != "completed")
                yield event
        finally:
            listening = False

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
        self._reject_driving_ids([instance_id for instance_id, _, _, _ in builders])
        if getattr(self._deps, "specialist_policy", None) is not None and builders:
            await self.own_run(str(builders[0][2].run_id))
            self._reject_driving_ids([instance_id for instance_id, _, _, _ in builders])
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
                if not pending:
                    break
                # The application owns the meaning of a deadline/event. Run
                # its callback serially with result handling, never inside an
                # event publisher or a detached background model task. With
                # no actors, return immediately rather than revive a held Run.
                deadline = await on_wakeup() if on_wakeup and not self._closed else None
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
                        yield InstanceEvent(
                            instance_id=instance_id,
                            kind="terminated",
                            runtime=base,
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001 - type only, never raw text
                        yield InstanceEvent(
                            instance_id=instance_id,
                            kind="failed",
                            runtime=base,
                            error=type(exc).__name__,
                        )
                        continue
                    yield InstanceEvent(
                        instance_id=instance_id,
                        kind="failed"
                        if final.phase is AgentPhase.FAILED
                        else "completed",
                        runtime=final,
                    )
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
