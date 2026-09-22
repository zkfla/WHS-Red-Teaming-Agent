"""PLAN node — Fake Recon Agent.

Emits exactly one hardcoded ``ActionProposal`` against the harmless
example MCP tool. This is the "Fake Recon Agent" from instruction.md
section 16 step 2/3 — no LLM call, no real recon logic.

TODO(agent-team): replace with an LLM-driven planner
(``deps.llm_client.complete_structured``) that reads Attack Graph state
via ``redteam.attack_graph`` and proposes real next actions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import AgentPhase
from redteam.core.ids import ActionId, EventId, GraphNodeId, new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes.observe import FAKE_AGENT_ID
from redteam.orchestration.state import GraphState


async def plan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]

    proposal = ActionProposal(
        action_id=ActionId(new_id("action")),
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        agent_id=FAKE_AGENT_ID,
        capability="recon.example",
        operation="ping",
        target=HostTarget(
            host="example.local",
            graph_node_id=GraphNodeId("example.local"),
        ),
        arguments={"message": "hello from fake recon agent"},
        reason="Stub recon agent probing example target to validate architecture wiring.",
        hypothesis_id=None,
        expected_observation="tool echoes the message back",
        created_at=datetime.now(timezone.utc),
    )

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.ACTION_PROPOSED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.plan",
            timestamp=datetime.now(timezone.utc),
            payload={"action_id": proposal.action_id, "capability": proposal.capability},
        )
    )

    new_runtime = runtime.model_copy(
        update={"phase": AgentPhase.POLICY, "pending_action_id": proposal.action_id}
    )
    return {**state, "runtime": new_runtime, "action_proposal": proposal}
