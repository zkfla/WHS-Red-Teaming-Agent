"""UPDATE node — records the target as a graph node.

Uses ``deps.graph_store`` (a ``core`` port) directly. Orchestration and
Attack Graph are both owned by Agent Team, so importing
``redteam.attack_graph.AttackGraphService`` here would also be fine
per instruction.md section 19 (same-team modules may share internals);
kept to the port here anyway to keep this node trivially testable with
just the Protocol.

TODO(agent-team): richer node/edge building (Service/Endpoint discovery,
Hypothesis -> Finding promotion), node dedup by identity instead of
overwrite-by-id. Also: ``target_graph_node_id`` returns ``None`` for
target kinds that don't carry a ``graph_node_id`` yet (Principal/Asset
targets without one set) — this node currently assumes it's always
present (true for the hardcoded Fake Recon Agent's ``HostTarget``);
a real planner needs to either always set it or handle ``None`` here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.target import target_display_name, target_graph_node_id
from redteam.core.enums import AgentPhase, GraphNodeType
from redteam.core.ids import EventId, new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState


async def update_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    assert proposal is not None and result is not None

    node_id = target_graph_node_id(proposal.target)
    assert node_id is not None, "Fake Recon Agent's HostTarget always sets graph_node_id"

    now = datetime.now(timezone.utc)
    node = GraphNode(
        id=node_id,
        type=GraphNodeType.HOST,
        labels=["observed-by-fake-recon-agent"],
        properties={
            "display_name": target_display_name(proposal.target),
            "last_tool_output": result.output,
        },
        created_at=now,
        updated_at=now,
    )
    await deps.graph_store.add_node(node)

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.GRAPH_UPDATED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.update",
            timestamp=datetime.now(timezone.utc),
            payload={"node_id": node.id},
        )
    )

    new_runtime = runtime.model_copy(update={"phase": AgentPhase.REPLAN})
    return {**state, "runtime": new_runtime}
