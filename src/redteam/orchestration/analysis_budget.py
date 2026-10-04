"""Request-local analysis budgets and fresh runtime admission checks."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.enums import PolicyVerdict
from redteam.orchestration.cognition import DecisionBudget, ReasoningHalted
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.delegation_decision import remaining_decisions
from redteam.orchestration.reasoning_time import reasoning_time_allowed
from redteam.orchestration.state import GraphState, TerminationReason


def verification_budget(state: GraphState, *, deps: RuntimeDeps) -> DecisionBudget:
    """Bound this review by the existing lifetime allowance, reserving cleanup.

    A spent allowance still creates a local counter with one slot; the
    per-request guard refuses its first request without consuming that slot.
    No decision or token allowance is issued to the delegation here.
    """
    step = state.get("plan_step")
    delegation_id = getattr(step, "delegation_id", None)
    delegation = (
        state["runtime"].delegations.get(delegation_id) if delegation_id is not None else None
    )
    allowance = deps.settings.decision_max_attempts
    if delegation is not None:
        allowance = min(allowance, max(remaining_decisions(delegation) - 1, 0))
    return DecisionBudget(max(allowance, 1))


def verification_guard(state: GraphState, *, deps: RuntimeDeps, budget: DecisionBudget):
    """Read fresh admission and time limits before each review request/retry."""

    async def before_call() -> None:
        runtime = state["runtime"]
        step = state.get("plan_step")
        if runtime.is_terminal or runtime.termination_reason in {
            TerminationReason.USER_INTERRUPTED,
            TerminationReason.OPERATOR_STOPPED,
            TerminationReason.OPERATOR_PAUSED,
        }:
            raise ReasoningHalted("analysis_run_stopped_or_paused")
        coordinator = deps.coordinator or coordinator_for(deps.task_graph_store, deps.path_store)

        async def inspect_current(change):
            if runtime.mission_id is None:
                change.legacy_scope(str(runtime.run_id))
                return
            change.binding(str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id))
            change.admit(str(runtime.run_id), str(runtime.mission_id))
            if step is not None:
                generation = change.generation(str(runtime.run_id), str(runtime.mission_id))
                epoch = str(generation) if generation else None
                if epoch != step.operator_intent_id:
                    raise ReasoningHalted("analysis_operator_instruction_changed")

        # The callback only reads the current canonical binding, mission and
        # intent generation. Store errors propagate to cognition's safe gate.
        await coordinator.change(inspect_current)
        now = datetime.now(timezone.utc)
        if not reasoning_time_allowed(deps.engagement, runtime, now=now):
            raise ReasoningHalted("analysis_engagement_time_exhausted")
        delegation_id = getattr(step, "delegation_id", None)
        if delegation_id is None or step is None:
            return
        delegation = runtime.delegations.get(delegation_id)
        proposal = state["action_proposal"]
        if (
            delegation is None
            or proposal is None
            or (
                delegation.path_id != step.path_id
                or delegation.path_step_id != step.path_step_id
                or delegation.mission_task_id != runtime.active_task_id
                or delegation.agent_id != str(proposal.agent_id)
            )
        ):
            raise ReasoningHalted("analysis_delegation_binding_mismatched")
        decision = state.get("policy_decision")
        resumed_approval = (
            delegation.status == "held"
            and delegation.termination_reason == TerminationReason.APPROVAL_PENDING
            and runtime.pending_approval_id is None
            and decision is not None
            and decision.verdict is PolicyVerdict.ALLOW
        )
        if delegation.status != "active" and not resumed_approval:
            raise ReasoningHalted("analysis_delegation_not_active")
        if delegation.budget.max_seconds is not None:
            started = delegation.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (now - started).total_seconds() >= delegation.budget.max_seconds:
                raise ReasoningHalted("analysis_delegation_time_exhausted")
        # The last lifetime decision remains available for final cleanup.
        if budget.used >= max(remaining_decisions(delegation) - 1, 0):
            raise ReasoningHalted("analysis_delegation_decisions_reserved_or_exhausted")
        max_tokens = delegation.budget.max_tokens
        if max_tokens is not None:
            if delegation.tokens_used is None:
                raise ReasoningHalted("analysis_delegation_token_usage_unknown")
            tokens_left = max(max_tokens - delegation.tokens_used, 0)
            if budget.token_halt_required(tokens_left):
                raise ReasoningHalted("analysis_delegation_token_limit_or_unknown_usage")

    return before_call
