"""REPLAN — continue the loop, or stop with a named reason."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventType
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.core.ids import EventId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.convergence import evaluate_convergence
from redteam.orchestration.events import REPLAN_TRIGGERED
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.nodes.observe import _task_agent
from redteam.orchestration.nodes.policy import (
    SEVERITY_ABORT_RUN,
    SEVERITY_REPLAN_GOAL,
    denial_severity,
)
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


def _abort_run_reason(decision: PolicyDecision | None) -> str:
    """Name why a Control ``ABORT_RUN`` denial ends the run.

    Budget exhaustion keeps its own exit reason; every other Control abort
    (a closed ROE time window, an unenforceable ROE policy, a stale approval)
    is reported as ``MISSION_BLOCKED`` -- the run cannot proceed.
    """

    if decision is not None and any(rule == "budget" for rule in decision.matched_rules):
        return TerminationReason.BUDGET_EXHAUSTED
    return TerminationReason.MISSION_BLOCKED


#: Task states the goal-blocking transition may start from. Any terminal
#: state is left untouched: a REJECTED approval can arrive after the work was
#: closed elsewhere, and blocking a settled Task raises.
_GOAL_BLOCKABLE = {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}


async def _block_goal_task(
    deps: RuntimeDeps,
    runtime: RuntimeState,
    decision: PolicyDecision | None,
    publisher: EventPublisher,
    last_event_id: EventId | None,
) -> tuple[EventId | None, bool]:
    """Block the active Task after Control declared its goal unviable.

    ``REPLAN_GOAL`` means the capability/action class itself is rejected, so
    the Task that requires it is not runnable. It is transitioned to BLOCKED
    (never FAILED -- nothing executed) and a different Task can be scheduled
    on the next OBSERVE. Returns ``(last_event_id, blocked)``.
    """

    assert runtime.active_task_id is not None
    task = await deps.task_graph_store.get_task(runtime.active_task_id)
    if task is None or task.status not in _GOAL_BLOCKABLE:
        # Already settled (or gone): leave it exactly as-is rather than crash
        # the replan with an invalid transition.
        return last_event_id, False
    reasons = "; ".join(decision.reasons) if decision is not None else "policy denial"
    await deps.task_graph_store.transition_task(
        task.task_id,
        TaskStatus.BLOCKED,
        now=datetime.now(timezone.utc),
        reason=f"policy rejected the goal: {reasons}",
    )
    event = await publisher.publish(
        EventType.TASK_BLOCKED,
        {"task_id": task.task_id, "reason": "policy_replan_goal"},
        causation_id=last_event_id,
    )
    return event.event_id, True


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
            # Exhausting a role's generic frontier does not prove a focused
            # dynamic objective (such as authorization comparison) succeeded.
            if runtime.mission_id is not None and runtime.active_task_id is not None:
                task = await deps.task_graph_store.get_task(runtime.active_task_id)
                if task is not None and task.status is TaskStatus.RUNNING:
                    unmet = task.provenance.source.startswith("replan:") or (
                        task.required and runtime.actions_by_agent.get(str(agent_id), 0) == 0
                    )
                    outcome = TaskStatus.BLOCKED if unmet else TaskStatus.COMPLETED
                    reason_text = "no candidate achieved the task objective" if unmet else None
                    await deps.task_graph_store.transition_task(
                        runtime.active_task_id,
                        outcome,
                        now=datetime.now(timezone.utc),
                        reason=reason_text,
                    )
                    completed = await publisher.publish(
                        EventType.TASK_BLOCKED if unmet else EventType.TASK_COMPLETED,
                        {"task_id": runtime.active_task_id, "agent_id": agent_id,
                         **({"reason": reason_text} if reason_text else {})},
                        causation_id=last_event_id,
                    )
                    last_event_id = completed.event_id

    reason = runtime.termination_reason or _hard_stop(runtime, deps)
    # Control tags a denial with the reaction it recommends. Orchestration
    # owns the reaction: implicit about *whether* an action passed, explicit
    # about what the run does next. An untagged denial keeps the historical
    # "block this action, replan" behavior.
    decision = state.get("policy_decision")
    severity = denial_severity(decision)
    if severity == SEVERITY_ABORT_RUN:
        # Nothing more can proceed (time window closed, budget exhausted,
        # stale approval). End now instead of re-planning impossible work
        # until the step ceiling. This is not an execution failure: the
        # failure counters are untouched.
        reason = _abort_run_reason(decision)
    if reason == TerminationReason.NO_ACTIONABLE_PLAN and runtime.mission_id is not None:
        # REPLAN may have just created a READY Task from the latest graph.
        # OBSERVE's earlier no-agent decision was made before that mutation.
        tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
        observation = state.get("observation")
        if observation is not None and any(
            task.status in {TaskStatus.READY, TaskStatus.RUNNING}
            and _task_agent(deps.agents, task, observation) is not None
            for task in tasks
        ):
            reason = None
    goal_blocked = False
    if (
        reason is None
        and severity == SEVERITY_REPLAN_GOAL
        and runtime.mission_id is not None
        and runtime.active_task_id is not None
    ):
        # The whole goal/capability is not viable, not just this one action:
        # block the Task so the run switches to a different goal instead of
        # re-proposing rejected work. A REJECTED approval carries this tag.
        last_event_id, goal_blocked = await _block_goal_task(
            deps, runtime, decision, publisher, last_event_id
        )
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
        elif runtime.mission_id is not None:
            tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
            next_agent = next(
                (
                    agent
                    for task in tasks
                    if task.status in {TaskStatus.READY, TaskStatus.RUNNING}
                    if observation is not None
                    if (agent := _task_agent(deps.agents, task, observation)) is not None
                ),
                None,
            )
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
                "termination_reason": None,
                # A goal-blocked Task must not remain the active one; OBSERVE
                # re-selects from the still-runnable Tasks.
                "active_task_id": None if goal_blocked else runtime.active_task_id,
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
