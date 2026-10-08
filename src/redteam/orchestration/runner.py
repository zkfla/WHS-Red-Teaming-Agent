"""Drives the compiled graph: loop config, checkpoint history, replay.

Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.core.ids import AgentInstanceId, EventId, MissionId, RunId
from redteam.orchestration.checkpointing import build_checkpointer, open_checkpointer
from redteam.orchestration.component_binding import builtin_component_binding
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import CLOSED, ChangeSet, CoordinationError, coordinator_for
from redteam.orchestration.diagnostics import operational_exception_diagnostic
from redteam.orchestration.graph import build_graph
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.progress import canonical_graph_changed
from redteam.orchestration.runtime_observation import RuntimeObservation, append_runtime_observation
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
    resume_graph_state,
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


def _claim_durable_approval(path: str, run_id: str, approval_id: str) -> bool:
    """Atomically claim one approved action across CLI processes."""
    checkpoint = Path(path)
    # The checkpointer created this database before we reach the claim gate.
    with sqlite3.connect(checkpoint, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS redteam_resume_claims ("
            "run_id TEXT NOT NULL, approval_id TEXT NOT NULL, "
            "PRIMARY KEY (run_id, approval_id))"
        )
        cursor = db.execute(
            "INSERT OR IGNORE INTO redteam_resume_claims (run_id, approval_id) VALUES (?, ?)",
            (run_id, approval_id),
        )
        db.commit()
        return cursor.rowcount == 1


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
        selected = self._deps.component_binding
        recorded = runtime.component_binding
        if recorded is None:
            # Old checkpoints only describe the built-in assembly. They
            # cannot recover a custom assembly that was never recorded.
            if selected is not None and selected != builtin_component_binding():
                return False
        elif recorded != selected:
            return False
        if checkpoint is not None:
            saved = checkpoint.values.get("runtime") if checkpoint.values else None
            fields = (
                "run_id",
                "instance_id",
                "phase",
                "mission_id",
                "engagement_id",
                "active_task_id",
                "termination_reason",
                "pending_action_id",
                "pending_approval_id",
                "active_path_id",
                "active_path_version",
                "last_progress_fingerprint",
                "step_count",
                "actions_executed",
                "retry_tool_calls",
                "model_requests_used",
                "model_tokens_used",
                "model_usage_unknown",
                "unresolved_evidence_ids",
                "evidence_reads",
                "local_tool_result_ids",
                "component_binding",
                "delegations",
            )
            if not isinstance(saved, RuntimeState) or self._snapshot_replays_execution(checkpoint):
                return False
            if any(getattr(saved, name) != getattr(runtime, name) for name in fields):
                return False
        if runtime.mission_id is None:
            coordinator = self._deps.coordinator or coordinator_for(
                self._deps.task_graph_store, self._deps.path_store
            )

            async def legacy(change: ChangeSet):
                return ("binding", str(runtime.run_id)) not in change.records

            return await coordinator.change(legacy)
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store
        )

        async def read(change: ChangeSet):
            return change.records.get(("binding", str(runtime.run_id)))

        bound = await coordinator.change(read)
        if bound is None:
            saved = checkpoint.values.get("runtime") if checkpoint and checkpoint.values else None
            trusted_checkpoint = (
                isinstance(saved, RuntimeState)
                and not self._snapshot_replays_execution(checkpoint)
                and saved.run_id == runtime.run_id
                and saved.mission_id == runtime.mission_id
                and saved.engagement_id == runtime.engagement_id
            )
            mission = await self._deps.task_graph_store.get_mission(runtime.mission_id)
            trusted_start = (
                fresh
                and runtime.step_count == 0
                and runtime.termination_reason is None
                and runtime.phase is AgentPhase.OBSERVE
                and mission is not None
                and mission.status is MissionStatus.ACTIVE
            )
            if not (trusted_checkpoint or trusted_start):
                return False
            if runtime.active_task_id:
                task = await self._deps.task_graph_store.get_task(runtime.active_task_id)
                if task is None or task.mission_id != runtime.mission_id:
                    return False
            try:
                await coordinator.bind(
                    str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id)
                )
            except CoordinationError:
                return False

        async def validate(change: ChangeSet):
            try:
                bound = change.binding(
                    str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id)
                )
            except CoordinationError:
                return False
            return not bound["closed"]

        return await coordinator.change(validate)

    async def recover_delivery(self, runtime: RuntimeState) -> int:
        """Recover committed same-run events without reviving investigation work."""
        if runtime.mission_id is None:
            return 0
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store
        )
        try:
            return await deliver_pending(
                coordinator,
                run_id=str(runtime.run_id),
                engagement_id=str(runtime.engagement_id),
                event_bus=self._deps.event_bus,
            )
        except CoordinationError:
            return 0

    async def _latest_allows_execution(self, runtime: RuntimeState, *, blocked=False) -> bool:
        if runtime.mission_id is None:
            return await self._binding_for(runtime)
        if not await self._binding_for(runtime):
            return False
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store
        )

        async def check(change: ChangeSet):
            mission = change.tasks.peek_mission(MissionId(str(runtime.mission_id)))
            if mission is None or mission.status in CLOSED:
                return False
            if mission.status is MissionStatus.BLOCKED:
                return (
                    blocked
                    and runtime.termination_reason in _RECONCILABLE_REASONS
                    and runtime.termination_reason != TerminationReason.APPROVAL_PENDING
                )
            return mission.status is MissionStatus.ACTIVE

        return await coordinator.change(check)

    async def _reactivate(self, runtime: RuntimeState) -> bool:
        if runtime.mission_id is None:
            return True
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store
        )

        async def activate(change: ChangeSet):
            bound = change.binding(
                str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id)
            )
            mission = change.tasks.peek_mission(MissionId(str(runtime.mission_id)))
            if bound["closed"] or mission is None or mission.status in CLOSED:
                return False
            if change.pending(str(runtime.run_id), str(runtime.mission_id)):
                return False
            if (
                mission.status is MissionStatus.BLOCKED
                and runtime.termination_reason in _RECONCILABLE_REASONS
                and runtime.termination_reason != TerminationReason.APPROVAL_PENDING
            ):
                await change.tasks.update_mission(
                    mission.model_copy(
                        update={
                            "status": MissionStatus.ACTIVE,
                            "revision": mission.revision + 1,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                )
            elif mission.status is not MissionStatus.ACTIVE:
                return False
            return True

        return await coordinator.change(activate)

    async def run(
        self, runtime: RuntimeState, *, initial_event_id: EventId | None = None
    ) -> RuntimeState:
        """Run the OBSERVE..REPLAN loop until a terminal phase."""
        if not await self._binding_for(runtime, fresh=True):
            raise CoordinationError("run_binding_missing_or_mismatched")
        return await self._run_state(
            runtime, initial_graph_state(runtime, last_event_id=initial_event_id)
        )

    async def _run_state(self, runtime: RuntimeState, graph_state: GraphState) -> RuntimeState:
        """Shared invocation path for a fresh run and an approval resume
        (``reconcile_once`` builds ``graph_state`` via ``resume_graph_state``
        instead of ``initial_graph_state`` for the latter) — same task
        tracking, cancellation and failure handling either way.
        """
        if not await self._latest_allows_execution(runtime):
            raise CoordinationError("mission_not_active")
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
        """Persist hard termination before returning control to app settlement."""
        if (
            runtime.instance_id is not None
            or runtime.mission_id is None
            or reason is None
            or reason in RECONCILABLE_REASONS
        ):
            return
        coordinator = self._deps.coordinator or coordinator_for(
            self._deps.task_graph_store, self._deps.path_store
        )

        async def close(change: ChangeSet):
            change.binding(
                str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id)
            )["closed"] = True

        await coordinator.change(close)

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
        work_ids = await self._deps.work_store.abandon_run(
            runtime.run_id,
            owner=runtime.work_owner,
            now=datetime.now(timezone.utc),
            reason=reason,
        )
        graph = AttackGraphService(self._deps.graph_store)
        for work_id in work_ids:
            work = await self._deps.work_store.get(work_id)
            if work is None or work.hypothesis_id is None:
                continue
            hypothesis = await graph.get_hypothesis(str(work.hypothesis_id))
            if hypothesis is not None and hypothesis.status == "open":
                await graph.resolve_hypothesis(
                    str(work.hypothesis_id),
                    status="inconclusive",
                    step=runtime.step_count,
                )

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

        # A fresh runner may enter through reconciliation without ever
        # calling run(), which is where the checkpoint thread is normally
        # bound. Bind it from the terminal runtime before looking up the
        # state needed to resume an approval.
        self._thread_id = self._thread_id or _checkpoint_thread(
            runtime.run_id, runtime.instance_id
        )

        await self.recover_delivery(runtime)
        if not await self._latest_allows_execution(runtime, blocked=True):
            return ReconciliationResult(
                runtime=runtime, wakeup=WakeupBatch(triggered=False, reason="mission_not_active")
            )
        wakeup = await self.wait_for_change(runtime, timeout_seconds=timeout_seconds)
        if runtime.termination_reason == TerminationReason.APPROVAL_PENDING:
            resumed = await self._resume_pending_approval(runtime)
            if resumed is None:
                return ReconciliationResult(runtime=runtime, wakeup=wakeup)
            return ReconciliationResult(runtime=resumed, wakeup=wakeup, resumed=True)
        if runtime.termination_reason == TerminationReason.OPERATOR_PAUSED:
            mission = (
                await self._deps.task_graph_store.get_mission(runtime.mission_id)
                if runtime.mission_id is not None
                else None
            )
            if mission is None or mission.status is not MissionStatus.ACTIVE:
                return ReconciliationResult(runtime=runtime, wakeup=wakeup)
        if runtime.termination_reason not in _RECONCILABLE_REASONS:
            return ReconciliationResult(runtime=runtime, wakeup=wakeup)
        if (
            not wakeup.triggered
            and runtime.termination_reason in _REVISION_GATED_REASONS
            and not await self._canonical_graph_changed(runtime)
        ):
            return ReconciliationResult(runtime=runtime, wakeup=wakeup)

        if not await self._reactivate(runtime):
            return ReconciliationResult(runtime=runtime, wakeup=wakeup)
        resumed = runtime.model_copy(
            update={
                "phase": AgentPhase.OBSERVE,
                "termination_reason": None,
                "no_progress_turns": 0,
                "mission_status": MissionStatus.ACTIVE
                if runtime.mission_id
                else runtime.mission_status,
            }
        )
        return ReconciliationResult(
            runtime=await self.run(resumed),
            wakeup=wakeup,
            resumed=True,
        )

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
        snapshot = await self._latest_snapshot()
        if snapshot is None or not await self._binding_for(runtime, checkpoint=snapshot):
            await self.recover_delivery(runtime)
            return runtime
        await self.recover_delivery(runtime)
        if not await self._latest_allows_execution(runtime, blocked=True):
            return runtime
        if runtime.termination_reason == TerminationReason.APPROVAL_PENDING:
            # A control plane that exposes the durable-resume extension must
            # link the checkpointed proposal successfully before we re-enter;
            # one that does not (e.g. an in-process test double) falls through
            # to the same path ``reconcile_once`` uses.
            if hasattr(self._deps.control_plane, "register_pending_proposal"):
                if not await self._register_pending_checkpoint(runtime):
                    return runtime
            resumed = await self._resume_pending_approval(runtime)
            return resumed if resumed is not None else runtime
        if runtime.termination_reason not in _RECONCILABLE_REASONS:
            return runtime
        if runtime.termination_reason == TerminationReason.OPERATOR_PAUSED:
            mission = (
                await self._deps.task_graph_store.get_mission(runtime.mission_id)
                if runtime.mission_id is not None
                else None
            )
            if mission is None or mission.status is not MissionStatus.ACTIVE:
                return runtime
        if (
            runtime.termination_reason in _REVISION_GATED_REASONS
            and not await self._canonical_graph_changed(runtime)
        ):
            return runtime
        if not await self._reactivate(runtime):
            return runtime
        resume_runtime = runtime.model_copy(
            update={
                "phase": AgentPhase.OBSERVE,
                "termination_reason": None,
                "no_progress_turns": 0,
                "mission_status": MissionStatus.ACTIVE
                if runtime.mission_id
                else runtime.mission_status,
            }
        )
        return await self.run(resume_runtime)

    async def _register_pending_checkpoint(self, runtime: RuntimeState) -> bool:
        """Re-link the checkpointed proposal to its Approval, or fail closed.

        Returns ``False`` (never raises) when the pending approval/proposal
        cannot be linked safely: a missing/mismatched proposal, an unsafe
        snapshot, a control plane without the durable-resume extension, or a
        registration that itself rejects the pairing.
        """
        if not await self._latest_allows_execution(runtime):
            return False
        if runtime.pending_approval_id is None or runtime.pending_action_id is None:
            return False
        snapshot = await self._latest_snapshot()
        state = snapshot.values if snapshot is not None and snapshot.values else None
        proposal = state.get("action_proposal") if state is not None else None
        if (
            proposal is None
            or proposal.action_id != runtime.pending_action_id
            or proposal.run_id != runtime.run_id
            or proposal.instance_id != runtime.instance_id
            or proposal.engagement_id != runtime.engagement_id
            or self._snapshot_replays_execution(snapshot)
        ):
            return False
        register = getattr(self._deps.control_plane, "register_pending_proposal", None)
        if register is None:
            return False
        try:
            await register(runtime.pending_approval_id, proposal)
        except Exception:  # noqa: BLE001 - a bad link must never become a resume
            logger.exception(
                "refusing to resume: could not link checkpointed proposal to approval %s",
                runtime.pending_approval_id,
            )
            return False
        return True

    async def _resume_pending_approval(self, runtime: RuntimeState) -> RuntimeState | None:
        """Resume a run stopped at ``APPROVAL_PENDING``, once resolved.

        ``None`` means "not resumable right now" (still pending, or the
        checkpointed state this needs is unavailable) -- the caller keeps
        the existing terminal ``runtime`` unchanged, exactly like an
        un-triggered reconciliation tick for any other reason.
        """
        if not await self._latest_allows_execution(runtime):
            return None
        if runtime.pending_approval_id is None or runtime.pending_action_id is None:
            return None  # defensive: nothing to resume
        if runtime.mission_id is not None:
            mission = await self._deps.task_graph_store.get_mission(runtime.mission_id)
            # An approval only admits the held action; it is not an operator
            # instruction. It must never revive a Mission the operator has
            # paused (PAUSED), stopped (CANCELLED), or otherwise settled
            # (COMPLETED/FAILED/BLOCKED/DRAFT) -- require ACTIVE, exactly like
            # the OPERATOR_PAUSED resume path below. Fail closed on an
            # unreadable Mission too, rather than resuming against unknown
            # operator state.
            if mission is None or mission.status is not MissionStatus.ACTIVE:
                return None

        # Resuming needs the checkpointed ActionProposal/PlanStep
        # replan_node preserved; without checkpointing there is nothing to
        # resume from. Unlike resume_from/state_history (explicit operator
        # calls), this runs inside a periodic reconciliation loop, so it
        # stays quiet rather than raising -- the run just stays terminated.
        # Validated *before* asking Control for an outcome, so a checkpoint
        # that is missing, mismatched, or unsafe to replay cannot consume a
        # resolution or reserve budget on the way to being refused.
        snapshot = await self._latest_snapshot()
        state = snapshot.values if snapshot is not None and snapshot.values else None
        proposal = state.get("action_proposal") if state is not None else None
        if (
            state is None
            or proposal is None
            or proposal.action_id != runtime.pending_action_id
            or proposal.run_id != runtime.run_id
            or proposal.instance_id != runtime.instance_id
            or proposal.engagement_id != runtime.engagement_id
            or self._snapshot_replays_execution(snapshot)
        ):
            # The checkpoint this needs (preserved by replan_node's
            # terminal branch) is missing or does not match -- resuming
            # blind would risk re-executing against the wrong proposal.
            # Fail closed: leave the run exactly as terminated.
            return None

        checkpoint_runtime = state.get("runtime")
        if (
            not isinstance(checkpoint_runtime, RuntimeState)
            or checkpoint_runtime.run_id != runtime.run_id
            or checkpoint_runtime.instance_id != runtime.instance_id
            or checkpoint_runtime.engagement_id != runtime.engagement_id
            or checkpoint_runtime.mission_id != runtime.mission_id
            or checkpoint_runtime.termination_reason != TerminationReason.APPROVAL_PENDING
            or checkpoint_runtime.pending_approval_id != runtime.pending_approval_id
        ):
            return None

        outcome = await self._deps.control_plane.approval_outcome(runtime.pending_approval_id)
        if outcome is None:
            return None  # still pending

        work_id = state.get("current_work_id")
        if work_id is None:
            return None
        lease = await self._deps.work_store.acquire(
            work_id,
            owner=runtime.work_owner,
            now=datetime.now(timezone.utc),
            ttl=timedelta(seconds=self._settings.lease_ttl_seconds),
        )
        if lease is None:
            return None

        # A second process may have recovered the same safe checkpoint before
        # this approval was resolved. Its independent Control counters cannot
        # serialize the tool side effect: claim once in the shared checkpoint
        # database before either process re-enters POLICY. Keep the claim even
        # if execution raises, because the tool may already have run.
        try:
            if self._settings.checkpoint_backend == "sqlite":
                claimed = await asyncio.to_thread(
                    _claim_durable_approval,
                    self._settings.checkpoint_path,
                    str(runtime.run_id),
                    str(runtime.pending_approval_id),
                )
            else:
                claim = (
                    id(self._checkpointer),
                    str(runtime.run_id),
                    str(runtime.pending_approval_id),
                )
                claimed = claim not in _MEMORY_RESUME_CLAIMS
                if claimed:
                    _MEMORY_RESUME_CLAIMS.add(claim)
        except (OSError, sqlite3.Error):
            logger.exception("refusing approval resume: durable claim unavailable")
            await self._deps.work_store.release(
                work_id, owner=runtime.work_owner, now=datetime.now(timezone.utc)
            )
            return None
        if not claimed:
            await self._deps.work_store.release(
                work_id, owner=runtime.work_owner, now=datetime.now(timezone.utc)
            )
            return None

        resumed_runtime = runtime.model_copy(
            update={
                "phase": AgentPhase.POLICY,
                "termination_reason": None,
                "no_progress_turns": 0,
            }
        )
        seeded = resume_graph_state(
            resumed_runtime,
            action_proposal=proposal,
            plan_step=state.get("plan_step"),
            current_work_id=state.get("current_work_id"),
        )
        # Native approval resumes the original provider response as a whole:
        # completed siblings remain completed, pending siblings keep IDs.
        for key in ("native_calls", "native_messages", "native_outcome"):
            if key in state:
                seeded[key] = state[key]
        self._deps.batch_pending_approvals.discard(runtime.work_owner)
        return await self._run_state(resumed_runtime, seeded)

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

    async def _canonical_graph_changed(self, runtime: RuntimeState) -> bool:
        """Resume-gate adapter over the shared canonical change judgment.

        The judgment itself lives with the fingerprint owner so the stall
        consumers and this gate cannot drift apart.
        """
        return await canonical_graph_changed(self._deps, runtime)

    async def resume_from(self, checkpoint_id: str) -> RuntimeState:
        """Re-run from an earlier checkpoint of the same thread.

        Rewinds ``GraphState`` only; the Attack Graph and Evidence Store
        keep everything recorded so far.
        """
        self._require_checkpointing("resume_from")
        await self._ensure_graph()
        assert self._graph is not None
        snapshot = await self._graph.aget_state(self.config(checkpoint_id=checkpoint_id))
        if self._snapshot_replays_execution(snapshot):
            raise ValueError(
                "checkpoint would repeat an execution; collect its stored result first"
            )
        saved = snapshot.values.get("runtime") if snapshot.values else None
        if not isinstance(saved, RuntimeState) or not await self._binding_for(
            saved, checkpoint=snapshot
        ):
            raise CoordinationError("checkpoint_binding_mismatched")
        await self.recover_delivery(saved)
        if not await self._latest_allows_execution(saved):
            raise CoordinationError("mission_not_active")
        final: GraphState = await self._graph.ainvoke(
            None, config=self.config(checkpoint_id=checkpoint_id)
        )
        return final["runtime"]

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
