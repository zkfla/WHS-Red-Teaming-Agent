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
from redteam.orchestration.state import GraphState, RuntimeState, initial_graph_state
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

    @property
    def settings(self) -> OrchestrationSettings:
        return self._settings

    @property
    def checkpointing_enabled(self) -> bool:
        return self._settings.checkpointing

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
        run_key = str(runtime.run_id)
        if self._bound_run_id is not None and self._bound_run_id != run_key:
            raise ValueError("one AgentLoopRunner may execute only one run")
        self._bound_run_id = run_key
        await self._ensure_graph()
        self._thread_id = self._thread_id or str(runtime.run_id)
        assert self._graph is not None
        try:
            final: GraphState = await self._graph.ainvoke(
                initial_graph_state(runtime), config=self.config()
            )
        except BaseException as exc:
            reason = f"runner_error:{type(exc).__name__}"
            cleanup = self._abandon_active_work(runtime, reason=reason)
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup),
                    timeout=self._settings.drain_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - preserve the original run failure
                pass
            raise
        return final["runtime"]

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
        """

        wakeup = await self.wait_for_change(runtime, timeout_seconds=timeout_seconds)
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
