"""REPLAN — continue the loop, or stop with a named reason."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.convergence import evaluate_convergence
from redteam.orchestration.events import REPLAN_TRIGGERED
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.replanning import replan_task_graph
from redteam.orchestration.state import GraphState, RuntimeState, TerminationReason

#: Why a replan happened, in the Event Bus vocabulary rather than the run's
#: exit contract (TerminationReason). Only the reasons that actually imply a
#: *decision* have an entry; "we simply took another turn" is not one.
_REPLAN_REASON_BY_TERMINATION = {
    TerminationReason.MAX_STEPS_REACHED: "max_steps",
    TerminationReason.BUDGET_EXHAUSTED: "budget_limit",
    TerminationReason.CONSECUTIVE_FAILURES: "action_failed",
    TerminationReason.STALLED: "no_progress",
}


def _replan_reason(
    *, termination: str | None, reactivated: bool, switched: bool
) -> str:
    """Name the replan, or return "" when nothing meaningful happened."""

    if termination is not None:
        mapped = _REPLAN_REASON_BY_TERMINATION.get(termination)
        if mapped is not None:
            return mapped
        return "terminal_condition"
    if reactivated:
        return "graph_changed"
    if switched:
        return "new_candidate_available"
    return ""


def _hard_stop(runtime: RuntimeState, deps: RuntimeDeps) -> str | None:
    settings = deps.settings
    if runtime.step_count >= settings.max_steps:
        return TerminationReason.MAX_STEPS_REACHED
    if runtime.remaining_budget is not None and runtime.remaining_budget <= 0:
        return TerminationReason.BUDGET_EXHAUSTED
    if runtime.consecutive_failures >= settings.max_consecutive_failures:
        return TerminationReason.CONSECUTIVE_FAILURES
    if runtime.no_progress_turns >= settings.max_no_progress_turns:
        return TerminationReason.STALLED
    return None


async def replan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    publisher = publisher_for(state, deps, "orchestration.replan")
    last_event_id = state.get("last_event_id")

    # Dynamic replanning: grow the Task Graph from what the Attack Graph now
    # knows (e.g. an object-bound endpoint -> an authorization Task). New work
    # coincides with a graph change, so the "graph changed -> continue" path
    # below keeps the run going and OBSERVE schedules it Task-first.
    if runtime.mission_id is not None:
        last_event_id = await replan_task_graph(
            deps.task_graph_store,
            graph_service(deps),
            runtime.mission_id,
            publisher=publisher,
            last_event_id=last_event_id,
        )

    agent_id = runtime.current_agent
    finished_agents = list(runtime.finished_agents)
    revision_cursors = dict(runtime.agent_revision_cursors)
    # No proposal came out of PLAN: this agent has nothing left to offer.
    agent_exhausted = agent_id is not None and state.get("action_proposal") is None

    if agent_exhausted and agent_id is not None:
        newly_finished = agent_id not in finished_agents
        if newly_finished:
            finished_agents.append(agent_id)
        observation = state.get("observation")
        if observation is not None:
            revision_cursors[str(agent_id)] = deps.agents.revision_fingerprint(
                agent_id, observation
            )
        elif runtime.last_progress_fingerprint is not None:
            # Compatibility for old checkpoints without the transient
            # observation. A fresh OBSERVE will replace this cursor.
            revision_cursors[str(agent_id)] = runtime.last_progress_fingerprint
        if newly_finished:
            spec = deps.agents.get(agent_id)
            retired = await publisher.publish(
                EventType.AGENT_FINISHED,
                {
                    "agent_id": agent_id,
                    "agent_name": spec.name if spec else None,
                    "actions_executed": runtime.actions_by_agent.get(str(agent_id), 0),
                    "reason": "no further steps available",
                },
                causation_id=last_event_id,
            )
            last_event_id = retired.event_id
            # Task-first: the Agent finishing its frontier means its active
            # Task is done -- complete it now (before any termination decision)
            # so the Task Graph reflects progress and the Mission can settle.
            if runtime.mission_id is not None and runtime.active_task_id is not None:
                task = await deps.task_graph_store.get_task(runtime.active_task_id)
                if task is not None and task.status is TaskStatus.RUNNING:
                    await deps.task_graph_store.transition_task(
                        runtime.active_task_id,
                        TaskStatus.COMPLETED,
                        now=datetime.now(timezone.utc),
                    )
                    completed = await publisher.publish(
                        EventType.TASK_COMPLETED,
                        {"task_id": runtime.active_task_id, "agent_id": agent_id},
                        causation_id=last_event_id,
                    )
                    last_event_id = completed.event_id

    reason = runtime.termination_reason or _hard_stop(runtime, deps)
    next_agent = None
    if reason in {TerminationReason.NO_AGENTS, TerminationReason.NO_ACTIONABLE_PLAN}:
        convergence = await evaluate_convergence(
            has_runnable_agent=False,
            graph=graph_service(deps),
            work_store=deps.work_store,
            run_id=runtime.run_id,
            no_agent_reason=reason,
        )
        reason = convergence.reason
    elif reason is None:
        observation = state.get("observation")
        proposal = state.get("action_proposal")
        # A resumed/legacy checkpoint may reach REPLAN without retaining its
        # transient observation.  A completed proposal proves the loop had
        # runnable work, so force one fresh OBSERVE before judging quiescence.
        if observation is None and proposal is not None:
            next_agent = deps.agents.get(proposal.agent_id)
        else:
            next_agent = deps.agents.next_agent(
                current=agent_id,
                finished=finished_agents,
                observation=observation,
                phase=AgentPhase.OBSERVE,
                finished_revisions=revision_cursors,
            )
        convergence = await evaluate_convergence(
            has_runnable_agent=next_agent is not None,
            graph=graph_service(deps),
            work_store=deps.work_store,
            run_id=runtime.run_id,
        )
        if convergence.terminal:
            reason = convergence.reason

    # Was a previously retired agent handed work again because *its own*
    # planning inputs changed? That -- not merely taking another turn -- is
    # what makes this a replan worth an event. The cursor was written from
    # the role-scoped, content-sensitive revision fingerprint, so an
    # unrelated node elsewhere cannot trigger it.
    reactivated = False
    observation = state.get("observation")
    if next_agent is not None and observation is not None:
        cursor = revision_cursors.get(str(next_agent.agent_id))
        reactivated = (
            next_agent.agent_id in finished_agents
            and cursor is not None
            and cursor
            != deps.agents.revision_fingerprint(next_agent.agent_id, observation)
        )
    switched = (
        next_agent is not None
        and agent_id is not None
        and next_agent.agent_id != agent_id
    )
    event_reason = _replan_reason(
        termination=reason, reactivated=reactivated, switched=switched
    )

    if reason is not None:
        triggered = await publisher.publish(
            REPLAN_TRIGGERED,
            {
                "outcome": (
                    "wait_for_approval"
                    if reason == TerminationReason.APPROVAL_PENDING
                    else "terminate"
                ),
                "reason": event_reason,
                "agent_id": agent_id,
                "next_agent_id": None,
                "step": runtime.step_count,
            },
            causation_id=last_event_id,
        )
        last_event_id = triggered.event_id
        # APPROVAL_PENDING is not a dead end: AgentLoopRunner.reconcile_once
        # resumes it directly at POLICY once a human resolves the Approval
        # (see orchestration.state.resume_graph_state), and needs this exact
        # ActionProposal/PlanStep still in the checkpointed state to do that
        # without re-planning (which would mint a new, uncorrelated action).
        # Every other terminal reason clears them as before.
        preserve_pending_action = reason == TerminationReason.APPROVAL_PENDING
        return advance(
            state,
            runtime.model_copy(
                update={
                    "phase": (
                        AgentPhase.FAILED
                        if reason
                        in {
                            TerminationReason.INCOMPLETE_ACTIVE_WORK,
                            TerminationReason.UNRESOLVED_HYPOTHESES,
                            TerminationReason.CLEANUP_REQUIRED,
                        }
                        else AgentPhase.FINISHED
                    ),
                    "termination_reason": reason,
                    "finished_agents": finished_agents,
                    "agent_revision_cursors": revision_cursors,
                }
            ),
            last_event_id=last_event_id,
            action_proposal=state.get("action_proposal") if preserve_pending_action else None,
            policy_decision=state.get("policy_decision") if preserve_pending_action else None,
            tool_result=None,
            result_evidence_id=None,
            plan_step=state.get("plan_step") if preserve_pending_action else None,
            current_work_id=None,
            dispatch_items=[],
        )

    if reactivated:
        triggered = await publisher.publish(
            REPLAN_TRIGGERED,
            {
                "outcome": "continue",
                "reason": event_reason or "graph_changed",
                "agent_id": agent_id,
                "next_agent_id": next_agent.agent_id if next_agent is not None else None,
                "step": runtime.step_count,
            },
            causation_id=last_event_id,
        )
        last_event_id = triggered.event_id

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.OBSERVE,
                "finished_agents": finished_agents,
                "agent_revision_cursors": revision_cursors,
                "pending_action_id": None,
                "active_hypothesis_id": None,
            }
        ),
        last_event_id=last_event_id,
        action_proposal=None,
        policy_decision=None,
        tool_result=None,
        result_evidence_id=None,
        plan_step=None,
        current_work_id=None,
        dispatch_items=[],
    )
