"""REPLAN node — stub.

For this stage, always terminates the run after one pass (the minimal
vertical slice is a single Observe->Plan->Policy->Execute->Verify->Update
loop). ``RunFinished``/``RunStarted`` are published by
``redteam.app.bootstrap`` around the whole graph invocation, not here.

TODO(agent-team): real re-planning — loop back to OBSERVE while
``remaining_budget``/``retry_count`` allow, based on ``last_verdict``.
"""

from __future__ import annotations

from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState


async def replan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    new_runtime = runtime.model_copy(update={"phase": AgentPhase.FINISHED})
    return {**state, "runtime": new_runtime}
