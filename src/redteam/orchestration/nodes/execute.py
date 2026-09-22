"""EXECUTE node — only reachable after an ALLOW ``PolicyDecision``.

Builds the ``ToolInvocation`` (via ``redteam.core.convert.build_tool_invocation``,
which also refuses to build one from a non-ALLOW decision as a defense-in-depth
check) and calls ``deps.tool_gateway`` — the Capability Layer's single entry
point. Nothing upstream of this node holds a reference to a ``ToolGateway``
implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import AgentPhase
from redteam.core.ids import EventId, ToolId, new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState


async def execute_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    decision = state["policy_decision"]
    assert proposal is not None and decision is not None

    invocation = build_tool_invocation(
        proposal,
        decision,
        tool_id=ToolId("example.ping"),
        timeout_seconds=10.0,
    )

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.TOOL_EXECUTION_STARTED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.execute",
            timestamp=datetime.now(timezone.utc),
            payload={"invocation_id": invocation.invocation_id, "tool_id": invocation.tool_id},
        )
    )

    result = await deps.tool_gateway.invoke(invocation)

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=(
                EventType.TOOL_EXECUTION_FINISHED
                if result.success
                else EventType.TOOL_EXECUTION_FAILED
            ),
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.execute",
            timestamp=datetime.now(timezone.utc),
            payload={"invocation_id": result.invocation_id, "success": result.success},
        )
    )

    new_runtime = runtime.model_copy(update={"phase": AgentPhase.VERIFY})
    return {**state, "runtime": new_runtime, "tool_result": result}
