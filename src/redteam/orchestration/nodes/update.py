"""UPDATE — fold the outcome back into the Attack Graph.

Also runs for blocked actions (reached from POLICY as well as VERIFY):
nothing executed, but the attempt and the policy verdict are recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.state import GraphState
from redteam.orchestration.work.models import WorkStatus


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
    tool_succeeded = result is not None and result.success
    verification_exception = (
        result.metadata.get("verification_exception") if result is not None else None
    )
    verification_failed = bool(verification_exception)
    succeeded = tool_succeeded and not verification_failed

    # PLAN records the hypothesis before the policy gate so the question is
    # auditable. A blocked action never reaches VERIFY, therefore UPDATE must
    # close it explicitly or reporting remains permanently blocked by a fake
    # "open" hypothesis that was never tested.
    if blocked and proposal.hypothesis_id is not None:
        await service.resolve_hypothesis(
            str(proposal.hypothesis_id),
            status="inconclusive",
            step=runtime.step_count,
        )

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
        "last_tool_succeeded": tool_succeeded,
    }
    if verification_failed:
        facts["last_verification_failed"] = True
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

    work_id = state.get("current_work_id")
    if work_id is not None:
        if blocked:
            work_status = WorkStatus.BLOCKED
            reason = decision.verdict.value if decision is not None else "blocked"
        elif succeeded:
            work_status = WorkStatus.SUCCEEDED
            reason = None
        else:
            work_status = WorkStatus.FAILED
            if verification_failed:
                reason = f"verification_exception:{verification_exception}"
            else:
                reason = result.error.code if result is not None and result.error else "tool_failed"
        await deps.work_store.complete(
            work_id,
            owner=str(runtime.run_id),
            status=work_status,
            now=datetime.now(timezone.utc),
            reason=reason,
        )

    return advance(
        state,
        runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
        last_event_id=updated.event_id,
        current_work_id=None,
    )
