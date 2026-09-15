"""UPDATE — fold the outcome back into the Attack Graph.

Also runs for blocked actions (reached from POLICY as well as VERIFY):
nothing executed, but the attempt and the policy verdict are recorded.
"""

from __future__ import annotations

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.state import GraphState


async def update_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    decision = state.get("policy_decision")
    step = state.get("plan_step")
    assert proposal is not None

    service = graph_service(deps)
    publisher = publisher_for(state, deps, "orchestration.update")
    correlation_id = str(proposal.action_id)
    operation_key = f"{proposal.capability}/{proposal.operation}"
    blocked = result is None
    succeeded = result is not None and result.success

    provenance = Provenance(
        step=runtime.step_count,
        source=f"tool:{result.tool_id}" if result is not None else "control-plane",
        run_id=str(runtime.run_id),
        confidence=Confidence.MEASURED if succeeded else Confidence.INFERRED,
    )

    # Only a successful execution may assert a predicate: recording
    # "reachable" after a failed or blocked probe would make the graph
    # claim something nobody observed.
    observed_attrs = dict(step.observed_attrs) if step is not None and succeeded else {}
    facts: dict[str, object] = {
        "last_operation": operation_key,
        "last_operation_succeeded": succeeded,
    }
    if blocked:
        facts["last_operation_blocked"] = True
        facts["last_block_verdict"] = decision.verdict.value if decision else None
        facts["last_block_reasons"] = list(decision.reasons) if decision else []

    node = await service.observe_target(
        proposal.target,
        attrs=observed_attrs,
        facts=facts,
        labels=[f"observed-by:{proposal.agent_id}"],
        provenance=provenance,
        step=runtime.step_count,
    )

    await service.record_attempt(
        node.id, operation_key, step=runtime.step_count, provenance=provenance
    )

    updated = await publisher.publish(
        EventType.GRAPH_UPDATED,
        {
            "node_id": str(node.id),
            "node_type": node.type.value,
            "action_id": proposal.action_id,
            "attrs": observed_attrs,
            "blocked": blocked,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    return advance(
        state,
        runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
        last_event_id=updated.event_id,
    )
