"""Reduce one parallel dispatch batch into the durable runtime counters."""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph.types import Overwrite

from redteam.core.contracts.policy import PolicyDecision
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.core.ids import ActionId
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance
from redteam.orchestration.nodes.policy import SEVERITY_ABORT_RUN, SEVERITY_REPLAN_GOAL
from redteam.orchestration.state import GraphState, TerminationReason


async def aggregate_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    items = state.get("dispatch_items", [])
    all_outcomes = state.get("worker_outcomes", {})
    outcomes = [
        all_outcomes[str(item.proposal.action_id)]
        for item in items
        if str(item.proposal.action_id) in all_outcomes
    ]

    executed = [item for item in outcomes if item.executed]
    succeeded = [item for item in executed if item.succeeded]
    failed = [item for item in executed if item.succeeded is False]
    remaining_budget = runtime.remaining_budget
    if remaining_budget is not None:
        # Mirror ``execute_node``'s per-action accounting exactly: one slot per
        # executed action plus one per extra retry attempt. A worker ran
        # ``execute_node`` against its own isolated copy, so its local budget
        # (including the verification reserve below) was discarded with it.
        consumed = sum(1 + item.retry_attempts for item in executed)
        total_budget = runtime.actions_executed + remaining_budget
        remaining_budget = max(remaining_budget - consumed, 0)
        if deps.settings.enforce_verification_reserve:
            reserve = deps.settings.verification_reserve(total_budget)
            if remaining_budget <= reserve:
                # Leave the verification slice untouched, exactly as the
                # sequential path does; REPLAN then stops on BUDGET_EXHAUSTED.
                remaining_budget = 0

    consecutive_failures = runtime.consecutive_failures
    if succeeded:
        consecutive_failures = 0
    elif failed:
        consecutive_failures += len(failed)

    executed_ids = {item.action_id for item in executed}
    actions_by_agent = dict(runtime.actions_by_agent)
    for dispatch in items:
        if str(dispatch.proposal.action_id) not in executed_ids:
            continue
        agent_key = str(dispatch.proposal.agent_id)
        actions_by_agent[agent_key] = actions_by_agent.get(agent_key, 0) + 1

    last_with_verdict = next(
        (item for item in reversed(outcomes) if item.verdict is not None), None
    )
    last_with_event = next(
        (item for item in reversed(outcomes) if item.last_event_id is not None), None
    )
    pending_approval = next(
        (item for item in outcomes if item.approval_id is not None), None
    )
    pending_dispatch = next(
        (item for item in items if pending_approval and
         str(item.proposal.action_id) == pending_approval.action_id),
        None,
    )
    # Workers are isolated: their policy decisions otherwise disappear before
    # REPLAN. Keep the strongest denial's *redacted summary*, not raw Control
    # text, so a batch cannot silently drop an abort or goal-level block.
    strongest_denial = next(
        (item for item in outcomes if item.policy_verdict is PolicyVerdict.DENY
         and item.policy_severity == SEVERITY_ABORT_RUN),
        None,
    ) or next(
        (item for item in outcomes if item.policy_verdict is PolicyVerdict.DENY
         and item.policy_severity == SEVERITY_REPLAN_GOAL),
        None,
    )
    summary = (
        PolicyDecision(
            action_id=ActionId(strongest_denial.action_id),
            verdict=PolicyVerdict.DENY,
            reasons=[strongest_denial.policy_reason or "policy denial"],
            matched_rules=[
                f"severity:{strongest_denial.policy_severity}",
                *(["budget"] if strongest_denial.policy_budget_abort else []),
            ],
            evaluated_at=datetime.now(timezone.utc),
        )
        if strongest_denial is not None else None
    )

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.REPLAN,
                "actions_executed": runtime.actions_executed + len(executed),
                "retry_tool_calls": (
                    runtime.retry_tool_calls + sum(item.retry_attempts for item in executed)
                ),
                "consecutive_failures": consecutive_failures,
                "remaining_budget": remaining_budget,
                "actions_by_agent": actions_by_agent,
                "last_verdict": (
                    last_with_verdict.verdict if last_with_verdict else runtime.last_verdict
                ),
                "pending_action_id": (
                    pending_dispatch.proposal.action_id if pending_dispatch is not None else None
                ),
                "pending_approval_id": (
                    pending_approval.approval_id
                    if pending_approval is not None
                    else runtime.pending_approval_id
                ),
                "active_hypothesis_id": None,
                "termination_reason": (
                    TerminationReason.APPROVAL_PENDING
                    if pending_approval is not None
                    else runtime.termination_reason
                ),
            }
        ),
        last_event_id=last_with_event.last_event_id if last_with_event else None,
        action_proposal=(
            pending_dispatch.proposal if pending_dispatch is not None
            else state.get("action_proposal")
        ),
        plan_step=(
            pending_dispatch.plan_step if pending_dispatch is not None
            else state.get("plan_step")
        ),
        dispatch_items=[],
        # ``worker_outcomes`` has a merge reducer so parallel workers can write
        # independently.  A plain ``{}`` would be merged as a no-op and retain
        # every prior turn in checkpoints.  The coordinator has folded this
        # batch into durable counters, so replace the channel explicitly.
        worker_outcomes=Overwrite(value={}),
        policy_decision=summary,
        current_work_id=pending_dispatch.work_id if pending_dispatch else None,
    )
