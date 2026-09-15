"""Drives the compiled graph: loop config, checkpoint history, replay.

Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from redteam.core.enums import AgentPhase
from redteam.orchestration.checkpointing import build_checkpointer
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.graph import build_graph
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import GraphState, RuntimeState, initial_graph_state


class CheckpointSnapshot(BaseModel):
    """One super-step of a run, as recorded by the checkpointer."""

    checkpoint_id: str
    phase: AgentPhase | None = None
    step_count: int = 0
    actions_executed: int = 0
    next_nodes: list[str] = Field(default_factory=list)


class CheckpointingDisabledError(RuntimeError):
    """Raised when history/replay is requested from a runner without a saver."""


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
        self._checkpointer = (
            checkpointer
            if checkpointer is not None
            else build_checkpointer(enabled=self._settings.checkpointing)
        )
        self._graph = build_graph(deps, checkpointer=self._checkpointer)
        self._thread_id: str | None = self._settings.thread_id

    @property
    def settings(self) -> OrchestrationSettings:
        return self._settings

    @property
    def checkpointing_enabled(self) -> bool:
        return self._checkpointer is not None

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
        self._thread_id = self._thread_id or str(runtime.run_id)
        final: GraphState = await self._graph.ainvoke(
            initial_graph_state(runtime), config=self.config()
        )
        return final["runtime"]

    async def state_history(self) -> list[CheckpointSnapshot]:
        """Every checkpoint of the last run, newest first."""
        self._require_checkpointing("state_history")
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

    async def resume_from(self, checkpoint_id: str) -> RuntimeState:
        """Re-run from an earlier checkpoint of the same thread.

        Rewinds ``GraphState`` only; the Attack Graph and Evidence Store
        keep everything recorded so far.
        """
        self._require_checkpointing("resume_from")
        final: GraphState = await self._graph.ainvoke(
            None, config=self.config(checkpoint_id=checkpoint_id)
        )
        return final["runtime"]

    def _require_checkpointing(self, operation: str) -> None:
        if self._checkpointer is None:
            raise CheckpointingDisabledError(
                f"{operation} needs a checkpointer; construct the runner with "
                f"OrchestrationSettings(checkpointing=True)"
            )
