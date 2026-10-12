"""Observe data and operator instructions; strategy is decided in PLAN."""

from __future__ import annotations

from redteam.attack_graph.observation import GraphObservation
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState


def _task_agent(agents, task, observation: GraphObservation):
    assigned = getattr(task, "assigned_agent", None)
    return agents.get(assigned) if assigned else next(iter(agents.all()), None)


async def observe_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    from redteam.orchestration.central_runtime import observe
    return await observe(state, deps)
