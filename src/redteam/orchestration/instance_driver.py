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
from collections.abc import AsyncGenerator, Callable, Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

from redteam.core.enums import AgentPhase
from redteam.core.ids import AgentInstanceId
from redteam.orchestration.checkpointing import build_checkpointer, open_checkpointer
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph.claim import release_mission_task_claim
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

    async def open(self) -> None:
        """Take ownership of the shared checkpointer (idempotent)."""
        if self._opened:
            return
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
        if self._closed:
            return
        self._closed = True
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
        """Explicit, caller-verified handoff of an unfinished task claim.

        Return is NOT settlement: call this only when handing the task to
        the TASK-007 reconcile path (or another verified owner). Errors
        propagate -- this never fails silent, unlike best-effort cleanup.
        """
        if runtime.mission_id is None or runtime.active_task_id is None:
            return
        owner = work_owner_for(runtime.run_id, runtime.instance_id)
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store
        )
        task_id = runtime.active_task_id

        async def _release(change: Any) -> None:
            await release_mission_task_claim(
                change, task_id, owner=owner, now=datetime.now(timezone.utc)
            )

        await coordinator.change(_release)

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
        return self._drain_as_ready(builders)

    def _reject_driving_ids(self, instance_ids: list[Any]) -> None:
        driving = {record[0] for record in self._active.values()}
        for instance_id in instance_ids:
            if str(instance_id) in driving:
                raise ValueError(f"instance already driving: {instance_id}")

    async def _drain_as_ready(
        self, builders: _DriveBuilders
    ) -> AsyncGenerator[InstanceEvent, None]:
        """Yield owned task results first-completed-first; own the tasks.

        The driving reservation re-checks and the task creation below run
        back-to-back with no await between them: two iterators built while
        idle cannot both start the same instance, whichever is consumed
        second rejects.
        """
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
            while pending:
                done, _ = await asyncio.wait(
                    pending.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
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
