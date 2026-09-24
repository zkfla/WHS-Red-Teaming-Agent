"""Shared plumbing for the orchestration nodes. No decisions here."""

from __future__ import annotations

from redteam.attack_graph.service import AttackGraphService
from redteam.core.ids import EventId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState, RuntimeState


def publisher_for(state: GraphState, deps: RuntimeDeps, source: str) -> EventPublisher:
    runtime = state["runtime"]
    return EventPublisher(
        deps.event_bus,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        source=source,
    )


def graph_service(deps: RuntimeDeps) -> AttackGraphService:
    """Orchestration and the Attack Graph are the same team's modules, so
    the service (not just the raw port) is fair game here — it is what
    guarantees identity normalization and the secret guard run."""
    return AttackGraphService(deps.graph_store)


def advance(
    state: GraphState,
    runtime: RuntimeState,
    *,
    last_event_id: EventId | None = None,
    **transient: object,
) -> GraphState:
    """Return the next ``GraphState``: new runtime plus transient updates."""
    updated: GraphState = {**state, "runtime": runtime}
    if last_event_id is not None:
        updated["last_event_id"] = last_event_id
    updated.update(transient)  # type: ignore[typeddict-item]
    return updated
