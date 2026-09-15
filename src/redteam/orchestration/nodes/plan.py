"""PLAN — frontier -> planner -> one ``ActionProposal`` (+ Hypothesis).

The planner never reaches a tool: the only thing leaving this node is a
proposal for the Control Plane. No proposal is a normal outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import target_display_name
from redteam.core.enums import AgentPhase
from redteam.core.ids import ActionId, HypothesisId, new_id
from redteam.orchestration.agents.base import PlanningContext, available_steps
from redteam.orchestration.agents.planners import build_planner
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.state import GraphState


async def plan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    observation = state.get("observation")
    assert observation is not None, "plan_node runs after observe_node"

    agent = deps.agents.get(runtime.current_agent) if runtime.current_agent else None
    if agent is None:  # pragma: no cover - observe_node routes away first
        return advance(state, runtime.model_copy(update={"phase": AgentPhase.REPLAN}))

    service = graph_service(deps)
    candidates = await available_steps(
        agent, service.view(), observation, step=runtime.step_count
    )

    planner = build_planner(agent.planner_kind, deps.llm_client)
    step = await planner.plan(
        PlanningContext(
            agent=agent,
            observation=observation,
            candidates=candidates,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            step=runtime.step_count,
            llm=deps.llm_client,
            goal=runtime.current_goal,
        )
    )

    if step is None:
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
        )

    publisher = publisher_for(state, deps, "orchestration.plan")
    last_event_id = state.get("last_event_id")
    target = step.resolved_target()
    target_node_id = node_id_for_target(target)
    action_id = ActionId(new_id("action"))

    hypothesis_id: HypothesisId | None = None
    if step.hypothesis is not None:
        hypothesis = Hypothesis(
            hypothesis_id=new_id("hyp"),
            description=step.hypothesis.description,
            confidence=step.hypothesis.confidence,
            status="open",
            metadata={
                "agent_id": str(agent.agent_id),
                "step_id": step.step_id,
                "target": target_display_name(target),
                "target_node_id": str(target_node_id),
                "violation_if_confirmed": step.hypothesis.violation_if_confirmed,
                # Recorded before the action runs — this is what VERIFY
                # will be held to, rather than a check written afterwards.
                "expectation": step.expectation.model_dump(mode="json")
                if step.expectation
                else None,
                **step.hypothesis.metadata,
            },
        )
        await service.record_hypothesis(
            hypothesis,
            about=target_node_id,
            step=runtime.step_count,
            provenance=Provenance(
                step=runtime.step_count,
                source=f"agent:{agent.agent_id}",
                run_id=str(runtime.run_id),
                confidence=Confidence.ASSERTED,
            ),
        )
        hypothesis_id = HypothesisId(hypothesis.hypothesis_id)
        created = await publisher.publish(
            EventType.HYPOTHESIS_CREATED,
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "description": hypothesis.description,
                "agent_id": agent.agent_id,
                "target_node_id": str(target_node_id),
            },
            correlation_id=str(action_id),
            causation_id=last_event_id,
        )
        last_event_id = created.event_id

    proposal = ActionProposal(
        action_id=action_id,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        agent_id=agent.agent_id,
        capability=step.capability,
        operation=step.operation,
        target=target,
        arguments=step.arguments,
        reason=step.reason,
        hypothesis_id=hypothesis_id,
        expected_observation=step.expected_observation,
        created_at=datetime.now(timezone.utc),
    )

    proposed = await publisher.publish(
        EventType.ACTION_PROPOSED,
        {
            "action_id": proposal.action_id,
            "agent_id": agent.agent_id,
            "capability": proposal.capability,
            "operation": proposal.operation,
            "target": target_display_name(target),
            "step_id": step.step_id,
            "candidates": len(candidates),
        },
        correlation_id=str(action_id),
        causation_id=last_event_id,
    )

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.POLICY,
                "pending_action_id": proposal.action_id,
                "active_hypothesis_id": hypothesis_id,
            }
        ),
        last_event_id=proposed.event_id,
        plan_step=step,
        action_proposal=proposal,
    )
