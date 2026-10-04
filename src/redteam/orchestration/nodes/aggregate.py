"""Reduce one parallel dispatch batch into the durable runtime counters.

Delegated follow-up outcomes ride the same channel (plan 3.5.5) and fold
through the same formulas; per-delegation counts and stored evidence
references accumulate from this batch only, never recounting earlier ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from langgraph.types import Overwrite

from redteam.core.contracts.policy import PolicyDecision
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.core.ids import ActionId
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.delegation import DelegationState
from redteam.orchestration.dispatch import WorkerOutcome
from redteam.orchestration.nodes._support import advance
from redteam.orchestration.nodes.policy import SEVERITY_ABORT_RUN, SEVERITY_REPLAN_GOAL
from redteam.orchestration.runtime_observation import append_runtime_observation
from redteam.orchestration.state import GraphState, TerminationReason


async def aggregate_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    items = state.get("dispatch_items", [])
    all_outcomes = state.get("worker_outcomes", {})
    initial_outcomes = [
        all_outcomes[str(item.proposal.action_id)]
        for item in items
        if str(item.proposal.action_id) in all_outcomes
    ]
    # Delegated follow-ups arrive on the same channel with fresh action ids
    # (plan 3.5.5). Same formulas below; widened inputs only.
    _, followup_outcomes = split_batch_outcomes(all_outcomes, items)
    outcomes = [*initial_outcomes, *followup_outcomes]

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
    for outcome in followup_outcomes:
        if not outcome.executed or not outcome.agent_id:
            continue
        actions_by_agent[outcome.agent_id] = actions_by_agent.get(outcome.agent_id, 0) + 1

    last_with_verdict = next(
        (item for item in reversed(outcomes) if item.verdict is not None), None
    )
    last_with_event = next(
        (item for item in reversed(outcomes) if item.last_event_id is not None), None
    )
    pending_approval = next((item for item in outcomes if item.approval_id is not None), None)
    pending_dispatch = next(
        (
            item
            for item in items
            if pending_approval and str(item.proposal.action_id) == pending_approval.action_id
        ),
        None,
    )
    # Workers are isolated: their policy decisions otherwise disappear before
    # REPLAN. Keep the strongest denial's *redacted summary*, not raw Control
    # text, so a batch cannot silently drop an abort or goal-level block.
    strongest_denial = next(
        (
            item
            for item in outcomes
            if item.policy_verdict is PolicyVerdict.DENY
            and item.policy_severity == SEVERITY_ABORT_RUN
        ),
        None,
    ) or next(
        (
            item
            for item in outcomes
            if item.policy_verdict is PolicyVerdict.DENY
            and item.policy_severity == SEVERITY_REPLAN_GOAL
        ),
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
        if strongest_denial is not None
        else None
    )

    delegations = dict(runtime.delegations)
    for delegation_id, delegation in delegations.items():
        related = [item for item in outcomes if item.delegation_id == delegation_id]
        if related:
            delegations[delegation_id] = update_delegation_from_outcomes(delegation, related)
    next_runtime = runtime.model_copy(
        update={
            "phase": AgentPhase.REPLAN,
            "actions_executed": runtime.actions_executed + len(executed),
            "retry_tool_calls": (
                runtime.retry_tool_calls + sum(item.retry_attempts for item in executed)
            ),
            "consecutive_failures": consecutive_failures,
            "remaining_budget": remaining_budget,
            "actions_by_agent": actions_by_agent,
            "delegations": delegations,
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
    )
    # Fold each isolated worker's own records back in dispatch order so the
    # next decision turn sees the whole batch in a stable sequence.
    for outcome in outcomes:
        for observation in outcome.runtime_observations:
            next_runtime = append_runtime_observation(next_runtime, observation)

    return advance(
        state,
        next_runtime,
        last_event_id=last_with_event.last_event_id if last_with_event else None,
        action_proposal=(
            pending_dispatch.proposal
            if pending_dispatch is not None
            else state.get("action_proposal")
        ),
        plan_step=(
            pending_dispatch.plan_step if pending_dispatch is not None else state.get("plan_step")
        ),
        dispatch_items=[],
        # ``worker_outcomes`` has a merge reducer so parallel workers can write
        # independently.  A plain ``{}`` would be merged as a no-op and retain
        # every prior turn in checkpoints.  The coordinator has folded this
        # batch into durable counters, so replace the channel explicitly.
        worker_outcomes=Overwrite(value={}),
        policy_decision=summary,
        current_work_id=(
            pending_dispatch.work_id
            if pending_dispatch is not None
            else (pending_approval.work_id if pending_approval is not None else None)
        ),
    )


def split_batch_outcomes(
    all_outcomes: dict[str, WorkerOutcome], dispatch_items: Sequence[object]
) -> tuple[list[WorkerOutcome], list[WorkerOutcome]]:
    """Initial outcomes in dispatch order, then follow-ups in action-id order.

    The root reuses this to feed the same formulas from both the initial
    batch and the delegated follow-ups of this turn only. Earlier turns
    already left this channel through the explicit reset above, so no
    entry is ever counted twice.
    """

    items = list(dispatch_items or [])
    outcomes = dict(all_outcomes or {})
    initial = [
        outcomes[str(item.proposal.action_id)]
        for item in items
        if str(item.proposal.action_id) in outcomes
    ]
    covered = {str(item.proposal.action_id) for item in items}
    followups = [outcome for key, outcome in outcomes.items() if key not in covered]
    followups.sort(key=lambda outcome: str(outcome.action_id))
    return initial, followups


def update_delegation_from_outcomes(
    delegation: DelegationState, outcomes: Sequence[WorkerOutcome]
) -> DelegationState:
    """Fold this batch outcomes into one delegation. Pure, no I/O.

    Only entries naming this delegation count toward it, and only while
    it is unfinished; unknown ids never create state here. Executed
    actions accumulate, stored evidence ids merge without duplicates,
    and an approval parks the delegation until the existing resume
    path returns it to the same purpose and allowance.
    """

    mine = [
        outcome
        for outcome in outcomes
        if outcome.delegation_id is not None and outcome.delegation_id == delegation.delegation_id
    ]
    if not mine or delegation.status == "finished":
        return delegation
    consumed = sum(1 + max(outcome.retry_attempts, 0) for outcome in mine if outcome.executed)
    seen = list(delegation.evidence_ids)
    for outcome in mine:
        for evidence_id in outcome.evidence_ids:
            if evidence_id not in seen:
                seen.append(evidence_id)
    update: dict[str, object] = {
        "actions_used": delegation.actions_used + consumed,
        "evidence_ids": seen,
    }
    if delegation.status == "active" and any(outcome.approval_id is not None for outcome in mine):
        update["status"] = "held"
        update["termination_reason"] = "approval_pending"
    return delegation.model_copy(update=update)
