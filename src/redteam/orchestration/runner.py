"""Drives the compiled graph: loop config, checkpoint history, replay.

Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import AgentPhase
from redteam.orchestration.checkpointing import build_checkpointer, open_checkpointer
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.graph import build_graph
from redteam.orchestration.progress import observation_fingerprint
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
    resume_graph_state,
)
from redteam.orchestration.wakeup import EventWakeup, WakeupBatch


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

#: Every termination reason ``reconcile_once`` can act on -- the generic
#: reasons above plus ``APPROVAL_PENDING`` (handled by a dedicated branch,
#: see ``_resume_pending_approval``). Public so a host loop
#: (``orchestration.reconciliation.run_reconciliation_loop``) knows when to
#: stop polling a run instead of re-deriving this set itself.
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
            self._checkpointer = build_checkpointer(enabled=self._settings.checkpointing)
        else:
            self._checkpointer = None
        self._graph = (
            build_graph(deps, checkpointer=self._checkpointer)
            if self._settings.checkpoint_backend == "memory" or checkpointer is not None
            else None
        )
        self._thread_id: str | None = self._settings.thread_id
        self._bound_run_id: str | None = None
        self._wakeup: EventWakeup | None = None
        self._wakeup_run_id: str | None = None
        self._active_graph_task: asyncio.Task[GraphState] | None = None
        self._stop_requested = False
        self._failure_runtime: RuntimeState | None = None

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

    async def run(self, runtime: RuntimeState) -> RuntimeState:
        """Run the OBSERVE..REPLAN loop until a terminal phase."""
        return await self._run_state(runtime, initial_graph_state(runtime))

    async def _run_state(self, runtime: RuntimeState, graph_state: GraphState) -> RuntimeState:
        """Shared invocation path for a fresh run and an approval resume
        (``reconcile_once`` builds ``graph_state`` via ``resume_graph_state``
        instead of ``initial_graph_state`` for the latter) — same task
        tracking, cancellation and failure handling either way.
        """
        run_key = str(runtime.run_id)
        if self._bound_run_id is not None and self._bound_run_id != run_key:
            raise ValueError("one AgentLoopRunner may execute only one run")
        self._bound_run_id = run_key
        self._failure_runtime = None
        await self._ensure_graph()
        self._thread_id = self._thread_id or str(runtime.run_id)
        assert self._graph is not None
        try:
            self._active_graph_task = asyncio.create_task(
                self._graph.ainvoke(graph_state, config=self.config())
            )
            final: GraphState = await self._active_graph_task
        except asyncio.CancelledError:
            latest = await self._latest_runtime(runtime)
            reason = TerminationReason.USER_INTERRUPTED
            await self._cleanup_after_failure(latest, reason=reason)
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
            reason = f"runner_error:{type(exc).__name__}"
            await self._cleanup_after_failure(latest, reason=reason)
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
        return final["runtime"]

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
        self, runtime: RuntimeState, *, reason: str
    ) -> None:
        cleanup = self._abandon_active_work(runtime, reason=reason)
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup),
                timeout=self._settings.drain_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - preserve the original run failure
            pass

    async def _latest_runtime(self, fallback: RuntimeState) -> RuntimeState:
        """Recover the last completed super-step without masking a failure."""

        if self._graph is None or self._checkpointer is None:
            return fallback
        try:
            snapshot = await self._graph.aget_state(self.config())
            candidate = snapshot.values.get("runtime") if snapshot.values else None
            return candidate if isinstance(candidate, RuntimeState) else fallback
        except Exception:  # noqa: BLE001 - recovery is best effort
            return fallback

    async def _abandon_active_work(
        self,
        runtime: RuntimeState,
        *,
        reason: str,
    ) -> None:
        work_ids = await self._deps.work_store.abandon_run(
            runtime.run_id,
            owner=str(runtime.run_id),
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
        if self._bound_run_id is not None and self._bound_run_id != run_key:
            raise ValueError("one AgentLoopRunner may wait on only one run")
        self._bound_run_id = run_key
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
        if runtime.termination_reason == TerminationReason.APPROVAL_PENDING:
            resumed = await self._resume_pending_approval(runtime)
            if resumed is None:
                return ReconciliationResult(runtime=runtime, wakeup=wakeup)
            return ReconciliationResult(runtime=resumed, wakeup=wakeup, resumed=True)
        if runtime.termination_reason not in _RECONCILABLE_REASONS:
            return ReconciliationResult(runtime=runtime, wakeup=wakeup)
        if (
            not wakeup.triggered
            and runtime.termination_reason in _REVISION_GATED_REASONS
            and not await self._canonical_graph_changed(runtime)
        ):
            return ReconciliationResult(runtime=runtime, wakeup=wakeup)

        resumed = runtime.model_copy(
            update={
                "phase": AgentPhase.OBSERVE,
                "termination_reason": None,
                "no_progress_turns": 0,
            }
        )
        return ReconciliationResult(
            runtime=await self.run(resumed),
            wakeup=wakeup,
            resumed=True,
        )

    async def _resume_pending_approval(self, runtime: RuntimeState) -> RuntimeState | None:
        """Resume a run stopped at ``APPROVAL_PENDING``, once resolved.

        ``None`` means "not resumable right now" (still pending, or the
        checkpointed state this needs is unavailable) -- the caller keeps
        the existing terminal ``runtime`` unchanged, exactly like an
        un-triggered reconciliation tick for any other reason.
        """
        if runtime.pending_approval_id is None or runtime.pending_action_id is None:
            return None  # defensive: nothing to resume
        outcome = await self._deps.control_plane.approval_outcome(runtime.pending_approval_id)
        if outcome is None:
            return None  # still pending

        # Resuming needs the checkpointed ActionProposal/PlanStep
        # replan_node preserved; without checkpointing there is nothing to
        # resume from. Unlike resume_from/state_history (explicit operator
        # calls), this runs inside a periodic reconciliation loop, so it
        # stays quiet rather than raising -- the run just stays terminated.
        state = await self._latest_graph_state(runtime)
        proposal = state.get("action_proposal") if state is not None else None
        if (
            state is None
            or proposal is None
            or proposal.action_id != runtime.pending_action_id
        ):
            # The checkpoint this needs (preserved by replan_node's
            # terminal branch) is missing or does not match -- resuming
            # blind would risk re-executing against the wrong proposal.
            # Fail closed: leave the run exactly as terminated.
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
        )
        return await self._run_state(resumed_runtime, seeded)

    async def _latest_graph_state(self, runtime: RuntimeState) -> GraphState | None:
        """The full last-checkpointed state (not just ``runtime``), or
        ``None`` if checkpointing found nothing for this thread.

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
            snapshot = await self._graph.aget_state(self.config())
            return snapshot.values if snapshot.values else None
        except Exception:  # noqa: BLE001 - treat as unavailable, never crash reconciliation
            return None

    async def _canonical_graph_changed(self, runtime: RuntimeState) -> bool:
        observation = await AttackGraphService(self._deps.graph_store).observe(
            step=runtime.step_count,
            max_per_type=self._settings.observation_max_per_type,
            max_total=self._settings.observation_max_total,
        )
        return observation_fingerprint(observation) != runtime.last_progress_fingerprint

    async def resume_from(self, checkpoint_id: str) -> RuntimeState:
        """Re-run from an earlier checkpoint of the same thread.

        Rewinds ``GraphState`` only; the Attack Graph and Evidence Store
        keep everything recorded so far.
        """
        self._require_checkpointing("resume_from")
        await self._ensure_graph()
        assert self._graph is not None
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
