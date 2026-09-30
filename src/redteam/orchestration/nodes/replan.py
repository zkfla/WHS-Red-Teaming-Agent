"""Manage runtime limits; investigation changes are LLM decisions in PLAN."""
from __future__ import annotations

from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import REPLAN_TRIGGERED
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.nodes.policy import SEVERITY_ABORT_RUN, denial_severity
from redteam.orchestration.state import GraphState, RuntimeState, TerminationReason


def _hard_stop(runtime: RuntimeState, deps: RuntimeDeps) -> str | None:
    if runtime.step_count >= deps.settings.max_steps:
        return TerminationReason.MAX_STEPS_REACHED
    if runtime.remaining_budget is not None and runtime.remaining_budget <= 0:
        return TerminationReason.BUDGET_EXHAUSTED
    if runtime.consecutive_failures >= deps.settings.max_consecutive_failures:
        return TerminationReason.CONSECUTIVE_FAILURES
    if runtime.no_progress_turns >= deps.settings.max_no_progress_turns:
        return TerminationReason.STALLED
    return None


async def replan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    reason = runtime.termination_reason or _hard_stop(runtime, deps)
    decision = state.get("policy_decision")
    if denial_severity(decision) == SEVERITY_ABORT_RUN:
        reason = (TerminationReason.BUDGET_EXHAUSTED if decision and "budget" in decision.matched_rules
                  else TerminationReason.MISSION_BLOCKED)
    publisher = publisher_for(state, deps, "orchestration.replan")
    event = await publisher.publish(REPLAN_TRIGGERED, {
        "outcome": "terminate" if reason else "continue", "reason": reason or "review_results",
        "step": runtime.step_count, "path_id": runtime.active_path_id,
        "path_version": runtime.active_path_version,
    }, causation_id=state.get("last_event_id"))
    preserve = reason == TerminationReason.APPROVAL_PENDING
    feedback = list(runtime.planning_feedback)
    result = state.get("tool_result")
    if result is not None and not result.success:
        feedback.append("tool_failed:" + (result.error.code if result.error else "unknown"))
    if decision is not None and decision.reasons:
        feedback.extend(decision.reasons)
    if reason:
        failed = reason in {TerminationReason.PLANNING_ERROR, TerminationReason.EXECUTION_ERROR,
                            TerminationReason.NO_AGENTS, TerminationReason.CONSECUTIVE_FAILURES}
        return advance(state, runtime.model_copy(update={
            "phase": AgentPhase.FAILED if failed else AgentPhase.FINISHED,
            "termination_reason": reason, "planning_feedback": feedback[-10:],
        }), last_event_id=event.event_id,
            plan_step=state.get("plan_step") if preserve else None,
            action_proposal=state.get("action_proposal") if preserve else None,
            policy_decision=decision if preserve else None,
            current_work_id=state.get("current_work_id") if preserve else None,
            tool_result=None, result_evidence_id=None, dispatch_items=[])
    return advance(state, runtime.model_copy(update={
        "phase": AgentPhase.OBSERVE, "pending_action_id": None,
        "active_hypothesis_id": None, "termination_reason": None,
        "planning_feedback": feedback[-10:],
    }), last_event_id=event.event_id, plan_step=None, action_proposal=None,
        policy_decision=None, current_work_id=None, tool_result=None,
        result_evidence_id=None, dispatch_items=[])
