"""Compile only actions proposed by the LLM for an accepted investigation path."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.core.ids import ActionId, new_id
from redteam.orchestration.candidates.models import ExecutableCandidate
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.diagnostics import diagnostic_for_exception
from redteam.orchestration.dispatch import DispatchItem
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.events import AGENT_DECISION_RECORDED, MCP_TOOL_SELECTED
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.paths import PathError
from redteam.orchestration.scheduler import WorkScheduler
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import GraphState, RuntimeState, TerminationReason
from redteam.orchestration.work.models import WorkStatus


def _verification_batch_capacity(runtime: RuntimeState, settings: OrchestrationSettings) -> int:
    remaining = runtime.remaining_budget
    if remaining is None:
        return settings.max_actions_per_turn
    if not settings.enforce_verification_reserve:
        return remaining
    available = max(
        remaining - settings.verification_reserve(runtime.actions_executed + remaining), 0
    )
    if available <= 1 or not settings.retry_retryable_tool_failures:
        return available
    return max(1, available // (1 + settings.max_retries))


async def plan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    observation = state.get("observation")
    publisher = publisher_for(state, deps, "orchestration.plan")
    completed_before = (
        {
            task.task_id
            for task in await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
            if task.status is TaskStatus.COMPLETED
        }
        if runtime.mission_id
        else set()
    )
    engine = InvestigationEngine(deps)
    try:
        path, candidates, outcome = await engine.decide(
            runtime, observation, last_event_id=state.get("last_event_id")
        )
    except (DecisionFailure, PathError, ValueError, TypeError) as exc:
        diagnostic = diagnostic_for_exception(exc, stage="path_or_action")
        event = await publisher.publish(
            "PlanningFailed",
            {
                "kind": diagnostic.kind,
                "stage": diagnostic.stage,
                "error_type": type(exc).__name__,
                "diagnostic": diagnostic.model_dump(mode="json"),
            },
            causation_id=engine.last_event_id,
        )
        return advance(
            state,
            runtime.model_copy(
                update={
                    "phase": AgentPhase.REPLAN,
                    "termination_reason": TerminationReason.PLANNING_ERROR,
                    "planning_error": type(exc).__name__,
                    "planning_diagnostic": diagnostic,
                    "planning_diagnostic_history": [
                        *runtime.planning_diagnostic_history,
                        diagnostic,
                    ][-10:],
                }
            ),
            last_event_id=event.event_id,
            plan_step=None,
            action_proposal=None,
            dispatch_items=[],
        )
    updates: dict[str, Any] = {"planning_error": None, "planning_diagnostic": None}
    state = {**state, "last_event_id": engine.last_event_id}
    if path:
        updates.update(active_path_id=path.path_id, active_path_version=path.version)
    if outcome is not None:
        if outcome == "continue_planning":
            review_diagnostic = getattr(engine, "planning_diagnostic", None)
            if review_diagnostic is not None:
                updates["planning_diagnostic"] = review_diagnostic
                updates["planning_diagnostic_history"] = [
                    *runtime.planning_diagnostic_history,
                    review_diagnostic,
                ][-10:]
                exhausted = await publisher.publish(
                    "PlanningFailed",
                    {
                        "kind": review_diagnostic.kind,
                        "stage": review_diagnostic.stage,
                        "error_type": "DecisionFailure",
                        "diagnostic": review_diagnostic.model_dump(mode="json"),
                    },
                    causation_id=engine.last_event_id,
                )
                state = {**state, "last_event_id": exhausted.event_id}
            updates.update(phase=AgentPhase.REPLAN, termination_reason=None)
            return advance(
                state,
                runtime.model_copy(update=updates),
                plan_step=None,
                action_proposal=None,
                dispatch_items=[],
            )
        updates.update(
            phase=AgentPhase.REPLAN,
            termination_reason=TerminationReason.GOAL_ACHIEVED
            if outcome == "goal_achieved"
            else None
            if outcome == "task_completed"
            else TerminationReason.INVESTIGATION_HELD,
        )
        last_event_id = engine.last_event_id
        tasks = (
            await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
            if runtime.mission_id
            else []
        )
        for task in tasks:
            if task.status is not TaskStatus.COMPLETED or task.task_id in completed_before:
                continue
            completed = await publisher.publish(
                EventType.TASK_COMPLETED,
                {"mission_id": runtime.mission_id, "task_id": task.task_id},
                causation_id=last_event_id,
            )
            last_event_id = completed.event_id
        if outcome == "goal_achieved":
            settled = await publisher.publish(
                EventType.MISSION_UPDATED,
                {"mission_id": runtime.mission_id, "status": "completed"},
                causation_id=last_event_id,
            )
            last_event_id = settled.event_id
        return advance(
            state,
            runtime.model_copy(update=updates),
            last_event_id=last_event_id,
            plan_step=None,
            action_proposal=None,
            dispatch_items=[],
        )
    limit = min(
        deps.settings.max_actions_per_turn,
        deps.settings.global_concurrency,
        _verification_batch_capacity(runtime, deps.settings),
    )
    chosen: list[ExecutableCandidate] = []
    conflicts: set[str] = set()
    targets: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    for candidate in candidates:
        if len(chosen) >= limit:
            break
        definition = deps.agents.definition(candidate.draft.agent_id)
        if definition and role_counts[candidate.draft.agent_id] >= definition.max_concurrency:
            continue
        agent = deps.agents.get(candidate.draft.agent_id)
        if (
            agent is not None
            and agent.max_actions is not None
            and runtime.actions_by_agent.get(candidate.draft.agent_id, 0)
            + role_counts[candidate.draft.agent_id]
            >= agent.max_actions
        ):
            continue
        if conflicts & set(candidate.draft.conflict_keys):
            continue
        if any(
            targets[identity] >= deps.settings.per_target_concurrency
            for identity in candidate.draft.target_node_ids
        ):
            continue
        chosen.append(candidate)
        conflicts.update(candidate.draft.conflict_keys)
        targets.update(candidate.draft.target_node_ids)
        role_counts.update([candidate.draft.agent_id])
    if not chosen:
        return advance(
            state,
            runtime.model_copy(
                update={
                    **updates,
                    "phase": AgentPhase.REPLAN,
                    "termination_reason": TerminationReason.BUDGET_EXHAUSTED
                    if limit < 1
                    else TerminationReason.INVESTIGATION_HELD,
                }
            ),
            plan_step=None,
            action_proposal=None,
        )
    now = datetime.now(timezone.utc)
    await WorkScheduler(deps.work_store).enqueue(
        chosen,
        graph_revision=runtime.step_count,
        run_id=runtime.run_id,
        now=now,
        mission_task_id=runtime.active_task_id,
    )
    pending = {
        item.candidate_key: item
        for item in await deps.work_store.list_items(
            status=WorkStatus.PENDING, run_id=runtime.run_id
        )
    }
    service = graph_service(deps)
    dispatch = []
    last_event_id = engine.last_event_id
    for candidate in chosen:
        work = pending.get(candidate.candidate_key)
        if work is None:
            continue
        lease = await deps.work_store.acquire(
            work.work_id,
            owner=str(runtime.run_id),
            now=now,
            ttl=timedelta(seconds=deps.settings.lease_ttl_seconds),
        )
        if lease is None:
            continue
        step = candidate.plan_step.model_copy(update={"candidate_key": candidate.candidate_key})
        hypothesis_id = step.existing_hypothesis_id
        if step.hypothesis is not None and step.existing_hypothesis_id is None:
            hypothesis_id = work.hypothesis_id
            if hypothesis_id is None:
                raise ValueError("hypothesis execution record is missing")
            await service.record_hypothesis(
                Hypothesis(
                    hypothesis_id=str(hypothesis_id),
                    description=step.hypothesis.description,
                    confidence=step.hypothesis.confidence,
                    status="open",
                    metadata={
                        "agent_id": str(candidate.draft.agent_id),
                        "step_id": step.step_id,
                        "target_node_id": str(node_id_for_target(step.resolved_target())),
                        "violation_if_confirmed": step.hypothesis.violation_if_confirmed,
                        "expectation": step.expectation.model_dump(mode="json")
                        if step.expectation
                        else None,
                        "path_id": step.path_id,
                        "path_version": step.path_version,
                        "path_step_id": step.path_step_id,
                    },
                ),
                about=node_id_for_target(step.resolved_target()),
                step=runtime.step_count,
                provenance=Provenance(
                    step=runtime.step_count,
                    source="llm:hypothesis",
                    run_id=str(runtime.run_id),
                    confidence=Confidence.INFERRED,
                ),
            )
        proposal = ActionProposal(
            action_id=ActionId(new_id("action")),
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            agent_id=candidate.draft.agent_id,
            capability=step.capability,
            operation=step.operation,
            target=step.resolved_target(),
            arguments=step.arguments,
            reason=step.reason,
            hypothesis_id=hypothesis_id,
            expected_observation=step.expected_observation,
            created_at=now,
            availability_impact=step.availability_impact,
        )
        selected = await publisher.publish(
            AGENT_DECISION_RECORDED,
            {
                "action_id": proposal.action_id,
                "path_id": step.path_id,
                "path_version": step.path_version,
                "path_step_id": step.path_step_id,
                "operation": step.operation,
                "rationale": step.reason,
                "selection_basis": "llm_accepted_path",
            },
            correlation_id=str(proposal.action_id),
            causation_id=last_event_id,
        )
        bound = await publisher.publish(
            MCP_TOOL_SELECTED,
            {
                "action_id": proposal.action_id,
                "path_id": step.path_id,
                "path_version": step.path_version,
                "path_step_id": step.path_step_id,
                "tool_id": str(candidate.tool.tool_id),
                "server_id": candidate.tool.server_id,
                "tool_name": candidate.tool.tool_name,
                "risk_level": candidate.tool.risk_level.value,
                "health_status": candidate.tool.health_status.value,
            },
            correlation_id=str(proposal.action_id),
            causation_id=selected.event_id,
        )
        event = await publisher.publish(
            EventType.ACTION_PROPOSED,
            {
                "action_id": proposal.action_id,
                "agent_id": proposal.agent_id,
                "candidate_key": candidate.candidate_key,
                "work_id": work.work_id,
                "path_id": step.path_id,
                "path_version": step.path_version,
                "path_step_id": step.path_step_id,
                "operation": step.operation,
                "batch_size": len(chosen),
            },
            correlation_id=str(proposal.action_id),
            causation_id=bound.event_id,
        )
        last_event_id = event.event_id
        dispatch.append(
            DispatchItem(
                work_id=work.work_id,
                plan_step=step,
                proposal=proposal,
                last_event_id=event.event_id,
            )
        )
    if not dispatch:
        return advance(
            state,
            runtime.model_copy(
                update={
                    **updates,
                    "phase": AgentPhase.REPLAN,
                    "termination_reason": TerminationReason.INVESTIGATION_HELD,
                }
            ),
            plan_step=None,
            action_proposal=None,
            dispatch_items=[],
        )
    first = dispatch[0]
    deps.batch_pending_approvals.discard(runtime.run_id)
    updates.update(
        phase=AgentPhase.POLICY,
        pending_action_id=first.proposal.action_id,
        current_agent=first.proposal.agent_id,
        active_path_step_id=first.plan_step.path_step_id,
        active_hypothesis_id=first.proposal.hypothesis_id,
    )
    return advance(
        state,
        runtime.model_copy(update=updates),
        last_event_id=last_event_id,
        plan_step=first.plan_step,
        action_proposal=first.proposal,
        current_work_id=first.work_id,
        dispatch_items=dispatch if len(dispatch) > 1 else [],
    )
