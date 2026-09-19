"""REPLAN — continue the loop, or stop with a named reason."""

from __future__ import annotations

from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.convergence import evaluate_convergence
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.state import GraphState, RuntimeState, TerminationReason


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

    if reason is not None:
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
            action_proposal=None,
            policy_decision=None,
            tool_result=None,
            plan_step=None,
            current_work_id=None,
            dispatch_items=[],
        )

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
        plan_step=None,
        current_work_id=None,
        dispatch_items=[],
    )
