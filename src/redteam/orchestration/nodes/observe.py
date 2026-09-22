"""OBSERVE node — stub.

TODO(agent-team): pull recent Evidence/Attack Graph state relevant to
``current_goal`` instead of doing nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase
from redteam.core.ids import AgentId, EventId, new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState

FAKE_AGENT_ID = AgentId("fake-recon-agent")


async def observe_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.AGENT_STARTED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.observe",
            timestamp=datetime.now(timezone.utc),
            payload={"agent_id": FAKE_AGENT_ID},
        )
    )
    new_runtime = runtime.model_copy(
        update={
            "phase": AgentPhase.PLAN,
            "current_agent": FAKE_AGENT_ID,
            "step_count": runtime.step_count + 1,
        }
    )
    return {**state, "runtime": new_runtime}
