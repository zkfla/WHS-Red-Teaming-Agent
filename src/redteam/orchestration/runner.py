"""Drives the compiled graph: loop config, checkpoint history, replay.

Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

from redteam.core.enums import AgentPhase, MissionStatus, TaskStatus
from redteam.core.ids import AgentInstanceId, EventId, RunId
from redteam.orchestration.checkpointing import build_checkpointer, open_checkpointer
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import (
    CoordinationError,
    StateConflict,
)
from redteam.orchestration.diagnostics import operational_exception_diagnostic
from redteam.orchestration.graph import build_graph
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.runtime_observation import RuntimeObservation, append_runtime_observation
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
)
from redteam.orchestration.wakeup import EventWakeup, WakeupBatch

logger = logging.getLogger(__name__)

#: Checkpoint branches whose next super-step runs a tool. Replaying one could
#: repeat a side effect that may already have happened (the checkpoint is
#: written before the node's effect is durably observable), so a durable
#: recovery/auto-resume refuses to act on such a snapshot -- fail closed.
_UNSAFE_REPLAY_NODES = frozenset({"execute", "worker", "tools"})


def _checkpoint_thread(run_id: RunId | str, instance_id: AgentInstanceId | str | None) -> str:
    """One shared run may contain several independently checkpointed workers."""
    return str(run_id) if instance_id is None else f"{run_id}:instance:{instance_id}"


def _snapshot_pending_work_ids(values: Any) -> set[str]:
    """Work ids the snapshot still owes an outcome, from its own fields.

    Derived from the snapshot's existing Work/dispatch/native-envelope
    fields -- never from fixed tool names. Covers uncompleted native
    dispatches, the in-flight work reference, and legacy batch dispatches,
    whether models or raw mappings survived the serde.
    """
    pending: set[str] = set()

    def _take(candidate: Any) -> None:
        wid = (
            candidate.get("work_id")
            if isinstance(candidate, Mapping)
            else getattr(candidate, "work_id", None)
        )
        if isinstance(wid, str) and wid:
            pending.add(wid)

    if not isinstance(values, Mapping):
        return pending
    current = values.get("current_work_id")
    if isinstance(current, str) and current:
        pending.add(current)
    calls = values.get("native_calls")
    if isinstance(calls, (list, tuple)):
        for call in calls:
            if isinstance(call, Mapping):
                if call.get("completed") is True:
                    continue
                dispatch = call.get("dispatch")
            else:
                if getattr(call, "completed", False) is True:
                    continue
                dispatch = getattr(call, "dispatch", None)
            _take(dispatch)
    items = values.get("dispatch_items")
    if isinstance(items, (list, tuple)):
        for item in items:
            _take(item)
    return pending


async def _submitted_work_ids(deps: Any, runtime: RuntimeState) -> set[str]:
    """Work ids with a journaled effect-start submission for this instance.

    Same scope as the reconciler (run + instance metadata match); digest
    failures read as absent (conservative: uncovered until proved). A store
    failure also reads as absent, which holds rather than proceeds.
    """
    from redteam.orchestration.execution_journal import checked_receipt

    submitted: set[str] = set()
    try:
        records = await deps.evidence_store.list_for_run(runtime.run_id)
    except Exception:
        return submitted
    for record in records:
        metadata = getattr(record, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        if metadata.get("receipt_kind") != "execution_submission":
            continue
        if metadata.get("instance_id") != runtime.instance_id:
            continue
        try:
            data = checked_receipt(record)
        except Exception:
            continue
        work_id = data.get("work_id") if isinstance(data, Mapping) else None
        if isinstance(work_id, str) and work_id:
            submitted.add(work_id)
    return submitted


class CheckpointSnapshot(BaseModel):
    """One super-step of a run, as recorded by the checkpointer."""

    checkpoint_id: str
    phase: AgentPhase | None = None
    step_count: int = 0
    actions_executed: int = 0
    next_nodes: list[str] = Field(default_factory=list)


class CheckpointingDisabledError(RuntimeError):
    """Raised when history/replay is requested from a runner without a saver."""


class ReconciliationResult(BaseModel):
    runtime: RuntimeState
    wakeup: WakeupBatch
    resumed: bool = False


_RECONCILABLE_REASONS = frozenset(
    {
        TerminationReason.OPERATOR_PAUSED,
        TerminationReason.RESOURCE_WAIT,
        "no_actionable_plan",
        "no_agents_registered",
        "stalled_no_graph_progress",
        "incomplete_active_work",
        "unresolved_hypotheses",
        "cleanup_required",
    }
)

_REVISION_GATED_REASONS = frozenset(
    {
        "no_actionable_plan",
        "no_agents_registered",
        "stalled_no_graph_progress",
    }
)

# The memory saver can be shared by multiple runners within a process. Claims
# are never released: after a crash the tool may already have been invoked,
# so retrying the same approval would be unsafe without an operator review.
_MEMORY_RESUME_CLAIMS: set[tuple[int, str, str]] = set()




#: Every termination reason ``reconcile_once`` can act on -- the generic
#: reasons above plus ``APPROVAL_PENDING`` (handled by a dedicated branch,
#: see ``_resume_pending_approval``). Public so a caller that polls a
#: suspended run (e.g. ``commands.run._wait_for_approval``) knows when to
#: stop instead of re-deriving this set itself.
RECONCILABLE_REASONS = _RECONCILABLE_REASONS | frozenset({TerminationReason.APPROVAL_PENDING})


class AgentLoopRunner:
    """Drives one engagement run end to end."""

    def __init__(
        self,
        deps: RuntimeDeps,
        *,
        settings: OrchestrationSettings | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        if deps.central_state is None:
            raise ValueError("runtime_requires_central_state")
        self._deps = deps
        self._settings = settings or deps.settings
        self._owns_checkpointer = checkpointer is None
        self._checkpointer_context: Any | None = None
        if checkpointer is not None:
            self._checkpointer = checkpointer
        elif self._settings.checkpoint_backend == "memory":
            self._checkpointer = build_checkpointer(
                enabled=self._settings.checkpointing, registry=deps.state_codecs
            )
        else:
            self._checkpointer = None
        self._graph = (
            build_graph(deps, checkpointer=self._checkpointer)
            if self._settings.checkpoint_backend == "memory" or checkpointer is not None
            else None
        )
        self._thread_id: str | None = self._settings.thread_id
        self._bound_thread_id: str | None = None
        self._wakeup: EventWakeup | None = None
        self._wakeup_run_id: str | None = None
        self._active_graph_task: asyncio.Task[GraphState] | None = None
        self._stop_requested = False
        self._failure_runtime: RuntimeState | None = None
        self._last_event_id: EventId | None = None
        self._central_resume_lock = asyncio.Lock()

    @property
    def settings(self) -> OrchestrationSettings:
        return self._settings

    @property
    def checkpointing_enabled(self) -> bool:
        return self._settings.checkpointing

    @property
    def failure_runtime(self) -> RuntimeState | None:
        """Best durable runtime recovered after the last failed invocation."""

        return self._failure_runtime

    @property
    def last_event_id(self) -> EventId | None:
        """Last event emitted by the graph, for terminal lifecycle causation."""

        return self._last_event_id

    @property
    def is_active(self) -> bool:
        return self._active_graph_task is not None and not self._active_graph_task.done()

    async def checkpoint_recovered_owner(self, original: RuntimeState,
                                         recovered: RuntimeState) -> None:
        """Keep original thread/calls while recording a verified Task owner epoch."""
        snapshot = await self._latest_snapshot()
        if (snapshot is None or snapshot.values.get("runtime") != original
                or original.run_id != recovered.run_id
                or original.instance_id != recovered.instance_id):
            raise CoordinationError("central_recovery_checkpoint_changed")
        await self._ensure_graph()
        assert self._graph is not None
        await self._graph.aupdate_state(self.config(), {"runtime": recovered}, as_node="settle")

    async def original_central_runtime(self, run_id, instance_id) -> RuntimeState:
        """Read latest original identity, including unsafe snapshots, without replay."""
        self._require_checkpointing("original_central_runtime")
        self._thread_id = self._thread_id or self._bind_runtime(run_id, instance_id)
        snapshot = await self._latest_snapshot()
        runtime = snapshot.values.get("runtime") if snapshot and snapshot.values else None
        if (not isinstance(runtime, RuntimeState) or str(runtime.run_id) != str(run_id)
                or str(runtime.instance_id) != str(instance_id)
                or not await self._binding_for(runtime, checkpoint=snapshot)):
            raise CoordinationError("central_original_checkpoint_missing_or_mismatched")
        return runtime

    def config(self, *, checkpoint_id: str | None = None) -> dict[str, Any]:
        configurable: dict[str, Any] = {}
        if self._checkpointer is not None:
            configurable["thread_id"] = self._thread_id or "run"
            if checkpoint_id is not None:
                configurable["checkpoint_id"] = checkpoint_id
        return {
            "configurable": configurable,
            "recursion_limit": self._settings.effective_recursion_limit,
        }

    def _bind_runtime(self, run_id: RunId | str, instance_id: AgentInstanceId | str | None) -> str:
        thread_id = _checkpoint_thread(run_id, instance_id)
        if self._bound_thread_id is not None and self._bound_thread_id != thread_id:
            raise ValueError("one AgentLoopRunner may execute only one run instance")
        self._bound_thread_id = thread_id
        return thread_id

    async def _binding_for(self, runtime: RuntimeState, *, checkpoint=None, fresh=False) -> bool:
        from redteam.orchestration.central_runtime import runtime_token

        try:
            await runtime_token(self._deps, runtime, admit=False)
        except CoordinationError:
            return False
        if checkpoint is not None:
            saved = checkpoint.values.get("runtime") if checkpoint.values else None
            if not isinstance(saved, RuntimeState) or saved != runtime:
                return False
            from redteam.orchestration.worker_conversation import validate_checkpoint_session

            try:
                await validate_checkpoint_session(self._deps, runtime, checkpoint.values)
            except (ValueError, TypeError) as exc:
                from redteam.orchestration.engine import DecisionRuleError

                code = (exc.diagnostic.code if isinstance(exc, DecisionRuleError)
                        else "worker_session_invalid")
                raise CoordinationError(code) from None
            return True
        return True

    async def recover_delivery(self, runtime: RuntimeState) -> int:
        """Recover committed same-run events without reviving investigation work."""
        if runtime.mission_id is None:
            return 0
        from redteam.orchestration.central_runtime import CentralEventBus

        bus = self._deps.event_bus
        if not isinstance(bus, CentralEventBus):
            raise CoordinationError("central_outbox_bus_missing")
        return await bus.flush()

    async def _latest_allows_execution(self, runtime: RuntimeState, *, blocked=False) -> bool:
        if not await self._binding_for(runtime):
            return False
        if runtime.mission_id is None:
            return False
        mission = await self._deps.central_state.get_mission(runtime.mission_id)
        return mission is not None and mission.status is MissionStatus.ACTIVE


    async def run(
        self, runtime: RuntimeState, *, initial_event_id: EventId | None = None
    ) -> RuntimeState:
        """Run the OBSERVE..REPLAN loop until a terminal phase."""
        if not await self._binding_for(runtime, fresh=True):
            raise CoordinationError("run_binding_missing_or_mismatched")
        self._thread_id = self._thread_id or self._bind_runtime(runtime.run_id, runtime.instance_id)
        snapshot = await self._latest_snapshot()
        if snapshot and any(snapshot.values.get(key) for key in (
            "worker_session", "native_messages", "native_calls",
        )):
            raise CoordinationError("worker_session_requires_original_resume")
        return await self._run_state(
            runtime, initial_graph_state(runtime, last_event_id=initial_event_id)
        )

    async def _run_state(self, runtime: RuntimeState, graph_state: GraphState) -> RuntimeState:
        task = await self._deps.central_state.get_task(str(runtime.active_task_id))
        with llm_request_scope(
            str(runtime.run_id), runtime.work_owner,
            task_id=(str(task.task_id)
                     if task is not None and task.probe_limits is not None else None),
        ):
            return await self._run_scoped_state(runtime, graph_state)

    async def _run_scoped_state(
        self, runtime: RuntimeState, graph_state: GraphState,
    ) -> RuntimeState:
        """Shared invocation path for a fresh run and an approval resume
        (``reconcile_once`` builds ``graph_state`` via ``resume_graph_state``
        instead of ``initial_graph_state`` for the latter) — same task
        tracking, cancellation and failure handling either way.
        """
        if not await self._latest_allows_execution(runtime):
            raise CoordinationError("mission_not_active")
        from redteam.orchestration.central_runtime import entering

        await entering(self._deps, runtime)
        thread_id = self._bind_runtime(runtime.run_id, runtime.instance_id)
        self._failure_runtime = None
        await self._ensure_graph()
        self._thread_id = self._thread_id or thread_id
        assert self._graph is not None
        try:
            self._active_graph_task = asyncio.create_task(
                self._graph.ainvoke(graph_state, config=self.config())
            )
            final: GraphState = await self._active_graph_task
            self._last_event_id = final.get("last_event_id")
        except asyncio.CancelledError:
            latest = await self._latest_runtime(runtime)
            reason = TerminationReason.USER_INTERRUPTED
            from redteam.orchestration.delegated_runtime import close_delegations

            latest = close_delegations(latest, reason)
            latest = await self._cleanup_after_failure(latest, reason=reason)
            await self._close_finished_binding(latest, reason=reason)
            if self._stop_requested:
                return latest.model_copy(
                    update={
                        "phase": AgentPhase.FINISHED,
                        "termination_reason": TerminationReason.USER_INTERRUPTED,
                        "pending_action_id": None,
                        "active_hypothesis_id": None,
                    }
                )
            self._failure_runtime = latest.model_copy(
                update={
                    "phase": AgentPhase.FAILED,
                    "termination_reason": TerminationReason.USER_INTERRUPTED,
                    "pending_action_id": None,
                    "active_hypothesis_id": None,
                }
            )
            raise
        except BaseException as exc:
            latest = await self._latest_runtime(runtime)
            latest = append_runtime_observation(
                latest,
                RuntimeObservation(
                    kind="state",
                    summary="Run execution failed before returning a terminal result.",
                    outcome="failed",
                    step=latest.step_count,
                    diagnostic=operational_exception_diagnostic(
                        exc, stage="runner", code="run_execution_failed",
                        summary="Run execution failed before returning a terminal result.",
                    ),
                ),
            )
            reason = f"runner_error:{type(exc).__name__}"
            from redteam.orchestration.delegated_runtime import close_delegations

            latest = close_delegations(latest, reason)
            latest = await self._cleanup_after_failure(latest, reason=reason)
            await self._close_finished_binding(latest, reason=TerminationReason.EXECUTION_ERROR)
            self._failure_runtime = latest.model_copy(
                update={
                    "phase": AgentPhase.FAILED,
                    "termination_reason": TerminationReason.EXECUTION_ERROR,
                    "pending_action_id": None,
                    "active_hypothesis_id": None,
                }
            )
            raise
        finally:
            self._active_graph_task = None
            self._stop_requested = False
        finished = final["runtime"]
        await self._close_finished_binding(finished, reason=finished.termination_reason)
        return finished

    async def _close_finished_binding(self, runtime: RuntimeState, *, reason: str | None) -> None:
        return  # The development worker cannot finalize the shared goal.

    def request_stop(self) -> bool:
        """Gracefully classify and stop the currently running graph invocation.

        Returns ``False`` when no run is active.  The caller should await the
        existing ``run()`` task; it returns a terminal ``USER_INTERRUPTED``
        runtime after active leases and hypotheses are closed.
        """

        task = self._active_graph_task
        if task is None or task.done():
            return False
        self._stop_requested = True
        task.cancel()
        return True

    async def _cleanup_after_failure(
        self, runtime: RuntimeState, *, reason: str,
    ) -> RuntimeState:
        cleanup = self._abandon_active_work(runtime, reason=reason)
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup),
                timeout=self._settings.drain_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original run failure
            return append_runtime_observation(
                runtime,
                RuntimeObservation(
                    kind="state", summary="Active work cleanup could not finish.",
                    outcome="cleanup_failed", step=runtime.step_count,
                    diagnostic=operational_exception_diagnostic(
                        exc, stage="cleanup", code="run_cleanup_failed",
                        summary="Active work cleanup could not finish.",
                    ),
                ),
            )
        return runtime

    async def _latest_runtime(self, fallback: RuntimeState) -> RuntimeState:
        """Recover the last completed super-step without masking a failure."""

        if self._graph is None or self._checkpointer is None:
            return fallback
        try:
            snapshot = await self._graph.aget_state(self.config())
            candidate = snapshot.values.get("runtime") if snapshot.values else None
            return candidate if isinstance(candidate, RuntimeState) else fallback
        except Exception as exc:  # noqa: BLE001 - recovery is best effort
            return append_runtime_observation(
                fallback,
                RuntimeObservation(
                    kind="state", summary="The last checkpoint could not be recovered.",
                    outcome="recovery_failed", step=fallback.step_count,
                    diagnostic=operational_exception_diagnostic(
                        exc, stage="checkpoint_recovery", code="checkpoint_recovery_failed",
                        summary="Checkpoint recovery failed; the initial state was used.",
                    ),
                ),
            )

    async def _abandon_active_work(
        self,
        runtime: RuntimeState,
        *,
        reason: str,
    ) -> None:
        from redteam.orchestration.central_runtime import abandon

        await abandon(self._deps, runtime)
        return

    async def state_history(self) -> list[CheckpointSnapshot]:
        """Every checkpoint of the last run, newest first."""
        self._require_checkpointing("state_history")
        await self._ensure_graph()
        assert self._graph is not None
        snapshots: list[CheckpointSnapshot] = []
        async for snapshot in self._graph.aget_state_history(self.config()):
            runtime = snapshot.values.get("runtime") if snapshot.values else None
            snapshots.append(
                CheckpointSnapshot(
                    checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
                    phase=runtime.phase if runtime is not None else None,
                    step_count=runtime.step_count if runtime is not None else 0,
                    actions_executed=runtime.actions_executed if runtime is not None else 0,
                    next_nodes=list(snapshot.next),
                )
            )
        return snapshots

    async def wait_for_change(
        self,
        runtime: RuntimeState,
        *,
        timeout_seconds: float,
    ) -> WakeupBatch:
        """Wait for a relevant event or return a reconciliation tick.

        The Event Bus port has no unsubscribe operation, so one runner owns one
        wakeup subscription for its run lifetime.
        """

        run_key = str(runtime.run_id)
        self._bind_runtime(runtime.run_id, runtime.instance_id)
        if self._wakeup is None:
            self._wakeup = EventWakeup(
                run_id=runtime.run_id,
                engagement_id=runtime.engagement_id,
            )
            self._wakeup_run_id = run_key
            await self._wakeup.attach(self._deps.event_bus)
        elif self._wakeup_run_id != run_key:
            raise ValueError("one AgentLoopRunner may wait on only one run")
        return await self._wakeup.wait(timeout_seconds=timeout_seconds)

    async def reconcile_once(
        self,
        runtime: RuntimeState,
        *,
        timeout_seconds: float,
    ) -> ReconciliationResult:
        """Wait once, then re-run a quiescent run against canonical stores.

        A timeout is also a reconciliation tick. Hard stops such as exhausted
        budget, step ceiling and repeated execution failure are never revived.

        ``APPROVAL_PENDING`` is handled separately from every other
        reconcilable reason (see ``_resume_pending_approval``): it never
        re-enters at OBSERVE, and it resumes only once the Approval is
        actually resolved -- an unrelated graph/evidence delta or a bare
        timeout must not touch it.
        """
        wakeup = await self.wait_for_change(runtime, timeout_seconds=timeout_seconds)
        central_resumed = await self.resume_recovered(runtime)
        return ReconciliationResult(
            runtime=central_resumed, wakeup=wakeup, resumed=central_resumed is not runtime,
        )

    async def checkpoint_exists(self, runtime: RuntimeState) -> bool:
        """Strict negative check for pre-entry recovery, never unavailable == absent.

        Unlike the best-effort recovery reader, store/codec errors propagate.
        Even an empty initial checkpoint requires the normal checkpoint path.
        """
        self._require_checkpointing("checkpoint_exists")
        self._thread_id = self._bind_runtime(runtime.run_id, runtime.instance_id)
        await self._ensure_graph()
        if self._checkpointer is None:
            raise CoordinationError("activation_checkpoint_store_unavailable")
        return await self._checkpointer.aget_tuple(self.config()) is not None

    async def recover_runtime(
        self, run_id: RunId | str, *, instance_id: AgentInstanceId | str | None = None
    ) -> RuntimeState | None:
        """Recover the durable terminal ``RuntimeState`` for a run or instance.

        Reads the latest checkpoint of the run's thread -- the same durable
        artifact a later ``redteam resume`` process rebuilds its runner
        against. Old runs use the run id as thread id; an instance uses a
        stable run+instance thread id so workers can resume independently.

        Returns ``None`` when the run has no checkpoint, the checkpoint
        cannot be read, the recovered ``run_id`` does not match, or the
        latest snapshot is unsafe to replay -- it still has EXECUTE/WORKER
        pending, so re-entering it could repeat a tool side effect that may
        already have happened. Callers decide whether to resume via
        ``resume_recovered``.
        """
        self._require_checkpointing("recover_runtime")
        run_key = str(run_id)
        self._thread_id = self._thread_id or self._bind_runtime(run_key, instance_id)
        snapshot = await self._latest_snapshot()
        if snapshot is None:
            return None
        values = snapshot.values if snapshot.values else None
        runtime = values.get("runtime") if values else None
        if (
            not isinstance(runtime, RuntimeState)
            or str(runtime.run_id) != run_key
            or (instance_id is not None and str(runtime.instance_id) != str(instance_id))
        ):
            return None
        if self._snapshot_replays_execution(snapshot):
            return None
        if not await self._binding_for(runtime, checkpoint=snapshot):
            return None
        return runtime

    async def resume_recovered(self, runtime: RuntimeState) -> RuntimeState:
        """Resume a ``recover_runtime`` result without waiting for an event.

        ``APPROVAL_PENDING`` first re-establishes the durable
        Approval<->proposal link (``DefaultControlPlane.register_pending_proposal``)
        from the safe checkpoint, then re-enters exactly at POLICY with the
        original ``ActionProposal``/``PlanStep`` -- never a freshly
        re-planned one. Any failure to link or validate fails closed: the run
        is returned exactly as terminated, never resumed against a guessed or
        mismatched action. Other reconcilable terminal reasons re-enter at
        OBSERVE; a hard stop is returned unchanged.
        """
        if self._central_resume_lock.locked() or self.is_active:
            raise StateConflict("central_actor_already_driving")
        async with self._central_resume_lock:
            return await self._continue_completed_central(runtime)

    async def resume_resource(self, runtime: RuntimeState) -> RuntimeState:
        """Continue this live Run's returned actor without rehydrating peer usage."""
        if self._central_resume_lock.locked() or self.is_active:
            raise StateConflict("central_actor_already_driving")
        async with self._central_resume_lock:
            return await self._continue_completed_central(runtime, rehydrate=False)

    async def _continue_completed_central(
        self, runtime: RuntimeState, *, rehydrate: bool = True,
    ) -> RuntimeState:
        """Resume only verified original calls from the latest actor checkpoint."""
        from redteam.core.ports.control_plane import DurableControlRecovery
        from redteam.orchestration.central_recovery import reconcile_originals
        from redteam.orchestration.central_runtime import actor_returned, worker_limit

        assert self._deps.central_state is not None
        snapshot = await self._latest_snapshot()
        if snapshot is None or not await self._binding_for(runtime, checkpoint=snapshot):
            raise CoordinationError("central_latest_checkpoint_binding_mismatched")
        await self.recover_delivery(runtime)
        control = self._deps.control_plane
        if not isinstance(control, DurableControlRecovery) or runtime.started_at is None:
            raise CoordinationError("durable_control_recovery_not_bound")
        if rehydrate:
            async with self._deps.policy_lock:
                await control.rehydrate_run(runtime.engagement_id, runtime.run_id,
                                            started_at=runtime.started_at)
        if not await self._latest_allows_execution(runtime):
            return runtime
        if runtime.termination_reason not in {
            None, TerminationReason.INVESTIGATION_HELD, TerminationReason.USER_INTERRUPTED,
            TerminationReason.EXECUTION_ERROR, TerminationReason.APPROVAL_PENDING,
            TerminationReason.RESOURCE_WAIT, TerminationReason.OPERATOR_PAUSED,
        }:
            return runtime
        outcome = await reconcile_originals(self._deps, runtime, snapshot=snapshot)
        if not outcome.ready:
            return outcome.runtime
        task = await self._deps.central_state.get_task(str(runtime.active_task_id))
        if task is None or task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            if task is not None and task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                from redteam.orchestration.nodes.react import (
                    append_completed_results,
                    restore_completed_calls,
                )
                from redteam.orchestration.worker_session import WorkerSession

                recovered = dict(snapshot.values)
                await restore_completed_calls(recovered, deps=self._deps)
                await append_completed_results(recovered, deps=self._deps)
                raw = recovered.get("worker_session")
                from redteam.orchestration.central_completion import unsafe

                executions = await self._deps.central_state.list_executions(
                    task_id=str(task.task_id),
                )
                if (raw is not None and not recovered.get("native_calls")
                        and not any(unsafe(item) for item in executions)):
                    session = WorkerSession.model_validate_json(json.dumps(raw))
                    recovered["worker_session"] = session.model_copy(update={
                        "status": "closed", "wait_reason": None,
                    }).checkpoint()
                    recovered["runtime"] = outcome.runtime.model_copy(update={
                        "phase": AgentPhase.FINISHED,
                        "termination_reason": (outcome.runtime.termination_reason
                                               or TerminationReason.INVESTIGATION_HELD),
                    })
                assert self._graph is not None
                await self._graph.aupdate_state(self.config(), recovered, as_node="settle")
                return recovered["runtime"]
            return outcome.runtime
        resumed = outcome.runtime.model_copy(update={
            "phase": AgentPhase.OBSERVE, "termination_reason": None,
        })
        seeded = dict(snapshot.values)
        seeded.update(runtime=resumed, native_outcome=None, native_final_text=None)
        await self._deps.central_state.transaction(lambda tx: tx.resume_actor(
            str(task.task_id), owner=runtime.work_owner, version=runtime.task_ownership_version,
            instance_id=str(runtime.instance_id), max_instances=worker_limit(self._deps),
        ))
        try:
            return await self._run_state(resumed, cast(GraphState, seeded))
        finally:
            await actor_returned(self._deps, resumed)


    async def recover_reconciling(
        self, run_id: RunId | str, *, instance_id: AgentInstanceId | str | None = None
    ) -> tuple[RuntimeState | None, ExecutionReconciliation | None]:
        """Recover identity/runtime; reconcile when the latest checkpoint is unsafe.

        Unknown, missing, mismatched, or binding-refused checkpoints yield
        ``(None, None)`` -- a runtime is never invented here. A safe latest
        checkpoint yields ``(runtime, None)`` for the existing safe path. An
        unsafe latest checkpoint (EXECUTE/WORKER/TOOLS pending) recovers the
        runtime, runs it through ``reconcile_execution_receipts`` (which
        never invokes tools), and returns ``(reconciled, reconciliation)``.
        Whether the caller may proceed is ``reconciliation.ready``: a
        pending-unknown or errored reconciliation stays held.

        The unsafe snapshot itself is never trusted for a NEW binding: the
        check below only accepts an EXISTING binding with full identity.
        The snapshot is also never re-entered (no ``astream(None)`` replay).
        """
        from redteam.orchestration.central_recovery import reconcile_originals

        self._require_checkpointing("recover_reconciling")
        self._thread_id = self._thread_id or self._bind_runtime(run_id, instance_id)
        snapshot = await self._latest_snapshot()
        runtime = snapshot.values.get("runtime") if snapshot and snapshot.values else None
        if (not isinstance(runtime, RuntimeState) or str(runtime.run_id) != str(run_id)
                or str(runtime.instance_id) != str(instance_id)
                or not await self._binding_for(runtime, checkpoint=snapshot)):
            raise CoordinationError("central_original_checkpoint_missing_or_mismatched")
        outcome = await reconcile_originals(self._deps, runtime, snapshot=snapshot)
        return runtime, outcome

    async def resume_reconciled(self, runtime: RuntimeState) -> RuntimeState:
        """Enter a reconciled runtime at a FRESH OBSERVE (never a replay).

        Only for runtimes whose reconciliation ``ready`` held true. Gates on
        live mission state exactly like the safe path, then starts a new
        reasoning turn from canonical stores.
        """
        return await self.resume_recovered(runtime)



    @staticmethod
    def _snapshot_replays_execution(snapshot: Any) -> bool:
        """Whether replaying ``snapshot`` would re-enter a tool execution."""
        next_nodes = getattr(snapshot, "next", None) or ()
        return bool(_UNSAFE_REPLAY_NODES.intersection(next_nodes))

    async def _latest_snapshot(self) -> Any | None:
        """The latest checkpointed ``StateSnapshot`` for this thread, or
        ``None`` if checkpointing is unavailable.

        Unlike the ``memory`` backend (built eagerly in ``__init__``), a
        ``sqlite`` checkpointer is only opened by ``_ensure_graph`` (an
        async context, since the SQLite saver owns a live connection) --
        which a caller reaching this method without first calling
        ``run()`` (e.g. a fresh runner built only to resume) never
        triggers on its own. Ensure it here too, or ``self._graph``/
        ``self._checkpointer`` are simply ``None`` and this silently
        reports "unavailable" forever, never actually checking the file.
        """
        try:
            await self._ensure_graph()
        except Exception:  # noqa: BLE001 - treat as unavailable, never crash reconciliation
            return None
        if self._graph is None or self._checkpointer is None:
            return None
        try:
            return await self._graph.aget_state(self.config())
        except Exception:  # noqa: BLE001 - treat as unavailable, never crash reconciliation
            return None


    async def resume_from(self, checkpoint_id: str) -> RuntimeState:
        """Re-run from an earlier checkpoint of the same thread.

        Rewinds ``GraphState`` only; the Attack Graph and Evidence Store
        keep everything recorded so far.
        """
        raise CoordinationError("central_checkpoint_rewind_is_not_supported")

    def _require_checkpointing(self, operation: str) -> None:
        if not self._settings.checkpointing:
            raise CheckpointingDisabledError(
                f"{operation} needs a checkpointer; construct the runner with "
                f"OrchestrationSettings(checkpointing=True)"
            )

    async def _ensure_graph(self) -> None:
        if self._graph is not None:
            return
        self._checkpointer_context = open_checkpointer(
            registry=self._deps.state_codecs,
            enabled=self._settings.checkpointing,
            backend=self._settings.checkpoint_backend,
            path=self._settings.checkpoint_path,
        )
        self._checkpointer = await self._checkpointer_context.__aenter__()
        self._graph = build_graph(self._deps, checkpointer=self._checkpointer)

    async def aclose(self) -> None:
        if self._checkpointer_context is not None:
            context = self._checkpointer_context
            self._checkpointer_context = None
            try:
                await context.__aexit__(None, None, None)
            finally:
                # The compiled graph retains the saver object. Reusing it after
                # SQLite's connection is closed fails far away from ``aclose``;
                # discard both so the next operation reopens the durable saver.
                self._graph = None
                self._checkpointer = None

    async def __aenter__(self) -> AgentLoopRunner:
        await self._ensure_graph()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()
