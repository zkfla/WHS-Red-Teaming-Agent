"""PLAN — frontier -> planner -> one ``ActionProposal`` (+ Hypothesis).

The planner never reaches a tool: the only thing leaving this node is a
proposal for the Control Plane. No proposal is a normal outcome.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import target_display_name
from redteam.core.enums import AgentPhase
from redteam.core.ids import ActionId, HypothesisId, new_id
from redteam.orchestration.agents.base import PlanningContext
from redteam.orchestration.agents.planners import build_planner
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.context_projection import (
    ContextProjector,
    project_evidence_excerpts,
)
from redteam.orchestration.difficulty import (
    apply_difficulty,
    estimate_difficulty,
    evaluate_branch_pruning,
)
from redteam.orchestration.dispatch import DispatchItem
from redteam.orchestration.events import AGENT_PROVIDER_FAILED, CANDIDATE_PRUNED
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.scheduler import WorkScheduler
from redteam.orchestration.state import GraphState
from redteam.orchestration.work.models import TERMINAL_STATUSES, WorkStatus


async def plan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    observation = state.get("observation")
    assert observation is not None, "plan_node runs after observe_node"

    agent = deps.agents.get(runtime.current_agent) if runtime.current_agent else None
    if agent is None:  # pragma: no cover - observe_node routes away first
        return advance(state, runtime.model_copy(update={"phase": AgentPhase.REPLAN}))
    if (
        agent.max_actions is not None
        and runtime.actions_by_agent.get(str(agent.agent_id), 0) >= agent.max_actions
    ):
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            dispatch_items=[],
        )

    service = graph_service(deps)
    now = datetime.now(timezone.utc)
    await deps.work_store.reclaim_expired(now=now)
    compiler = CandidateCompiler(deps.tool_catalog)
    definition = deps.agents.definition(agent.agent_id)
    provider = (
        deps.candidate_providers.get(definition.provider_id)
        if definition is not None
        else None
    )
    try:
        if definition is None:
            compiled = await compiler.compile_playbook(
                agent, service.view(), observation, step=runtime.step_count
            )
        else:
            if provider is None:
                raise LookupError(
                    f"candidate provider {definition.provider_id!r} is not registered"
                )
            drafts = await provider.propose(
                agent, service.view(), observation, step=runtime.step_count
            )
            compiled = compiler.compile_drafts(drafts)
    except Exception as exc:  # noqa: BLE001 - isolate one extension from other roles
        failed = await publisher_for(state, deps, "orchestration.plan").publish(
            AGENT_PROVIDER_FAILED,
            {
                "agent_id": agent.agent_id,
                "provider_id": definition.provider_id if definition else "playbook:fallback",
                "error_type": type(exc).__name__,
            },
            causation_id=state.get("last_event_id"),
        )
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            dispatch_items=[],
            last_event_id=failed.event_id,
        )
    history = await deps.work_store.list_items(run_id=runtime.run_id)
    terminal_keys = {
        item.candidate_key for item in history if item.status in TERMINAL_STATUSES
    }
    compiled = [
        candidate
        for candidate in compiled
        if candidate.plan_step.repeatable
        or candidate.candidate_key not in terminal_keys
    ]
    source_nodes = max(sum(observation.node_counts.values()), 1)
    if definition is None:
        context_load = 1.0
    else:
        projected_node_ceiling = min(
            definition.context_profile.max_nodes,
            max(1, definition.context_profile.max_tokens // 40),
        )
        context_load = min(projected_node_ceiling / source_nodes, 1.0)
    compiled = [
        apply_difficulty(
            candidate,
            estimate_difficulty(candidate, history, context_load=context_load),
        )
        for candidate in compiled
    ]
    publisher = publisher_for(state, deps, "orchestration.plan")
    last_event_id = state.get("last_event_id")
    eligible = []
    for candidate in compiled:
        prune = evaluate_branch_pruning(
            candidate,
            history,
            failure_threshold=deps.settings.branch_prune_failure_threshold,
            difficulty_threshold=deps.settings.branch_prune_difficulty_threshold,
            objective_override=deps.settings.branch_prune_objective_override,
        )
        if not prune.pruned:
            eligible.append(candidate)
            continue
        event = await publisher.publish(
            CANDIDATE_PRUNED,
            {
                "candidate_key": candidate.candidate_key,
                "agent_id": candidate.draft.agent_id,
                "target_node_ids": candidate.draft.target_node_ids,
                "reason": prune.reason,
                "related_failures": prune.related_failures,
                "estimated_difficulty": candidate.draft.signals.estimated_difficulty,
            },
            causation_id=last_event_id,
        )
        last_event_id = event.event_id
    compiled = eligible
    compiled.sort(key=lambda item: (-item.priority.total, item.candidate_key))
    planning_observation = observation
    if definition is not None and compiled:
        # Bound only after dedup and pruning, otherwise already-terminal or
        # pruned high-ranked candidates could hide untouched lower candidates.
        compiled = compiled[: definition.context_profile.max_candidates]
        planning_observation = await ContextProjector(
            service, deps.context_cache
        ).project(
            definition.context_profile,
            step=runtime.step_count,
            target_node_ids=[
                node_id
                for candidate in compiled
                for node_id in candidate.draft.target_node_ids
            ],
            fallback=observation,
            revision=runtime.step_count,
            cache_namespace=str(runtime.run_id),
        )
    candidates = [candidate.plan_step for candidate in compiled]
    evidence_excerpts = []
    if definition is not None and compiled and definition.context_profile.max_evidence:
        evidence_limit = min(
            definition.context_profile.max_evidence,
            max(1, definition.context_profile.max_tokens // 100),
        )
        evidence_excerpts = project_evidence_excerpts(
            await deps.evidence_store.list_for_run(runtime.run_id),
            target_node_ids=[
                node_id
                for candidate in compiled
                for node_id in candidate.draft.target_node_ids
            ],
            limit=evidence_limit,
        )

    planner = build_planner(agent.planner_kind, deps.llm_client)
    step = await planner.plan(
        PlanningContext(
            agent=agent,
            observation=planning_observation,
            candidates=candidates,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            step=runtime.step_count,
            llm=deps.llm_client,
            goal=runtime.current_goal,
            metadata={
                "context_profile": (
                    definition.context_profile.profile_id if definition else "default"
                ),
                "projected_nodes": sum(planning_observation.node_counts.values()),
                "source_nodes": sum(observation.node_counts.values()),
                "evidence_excerpts": [
                    item.model_dump(mode="json") for item in evidence_excerpts
                ],
            },
        )
    )

    if step is None:
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            last_event_id=last_event_id,
        )

    selected = next((item for item in compiled if item.plan_step == step), None)
    if selected is None:  # Defensive: a planner may only return an offered step.
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            last_event_id=last_event_id,
        )

    batch_limit = min(
        deps.settings.max_actions_per_turn,
        deps.settings.global_concurrency,
    )
    batch_limit = min(
        batch_limit,
        deps.agents.weighted_batch_limit(
            agent.agent_id,
            observation=observation,
            phase=AgentPhase.OBSERVE,
            capacity=batch_limit,
        ),
    )
    if definition is not None:
        batch_limit = min(batch_limit, definition.max_concurrency)
    if agent.max_actions is not None:
        batch_limit = min(
            batch_limit,
            agent.max_actions - runtime.actions_by_agent.get(str(agent.agent_id), 0),
        )
    if runtime.remaining_budget is not None:
        batch_limit = min(batch_limit, runtime.remaining_budget)
    if batch_limit < 1:
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            dispatch_items=[],
            last_event_id=last_event_id,
        )
    # Do not enqueue siblings that the ledger would immediately reject for
    # sharing a conflict key. That would leave unreachable PENDING rows behind.
    selected_candidates = [selected]
    occupied_conflicts = set(selected.draft.conflict_keys)
    target_counts = Counter(selected.draft.target_node_ids)
    for item in compiled:
        if len(selected_candidates) >= batch_limit:
            break
        if item.candidate_key == selected.candidate_key:
            continue
        conflicts = set(item.draft.conflict_keys)
        if conflicts & occupied_conflicts:
            continue
        if any(
            target_counts[target_id] >= deps.settings.per_target_concurrency
            for target_id in item.draft.target_node_ids
        ):
            continue
        selected_candidates.append(item)
        occupied_conflicts.update(conflicts)
        target_counts.update(item.draft.target_node_ids)

    await WorkScheduler(deps.work_store).enqueue(
        selected_candidates,
        graph_revision=runtime.step_count,
        run_id=runtime.run_id,
        now=now,
    )
    # Re-bind provider output to both newly inserted and previously pending
    # ledger rows. Existing PENDING rows are expected after an unsatisfied
    # dependency or a crash between enqueue and acquire; treating the store's
    # duplicate rejection as "no work" would orphan them forever.
    selected_keys = {item.candidate_key for item in selected_candidates}
    work_by_candidate = {
        item.candidate_key: item
        for item in await deps.work_store.list_items(
            status=WorkStatus.PENDING,
            run_id=runtime.run_id,
        )
        if item.candidate_key in selected_keys
    }
    leased = []
    for candidate in selected_candidates:
        work = work_by_candidate.get(candidate.candidate_key)
        if work is None:
            continue
        lease = await deps.work_store.acquire(
            work.work_id,
            owner=str(runtime.run_id),
            now=now,
            ttl=timedelta(seconds=deps.settings.lease_ttl_seconds),
        )
        if lease is not None:
            leased.append((candidate, work))

    if not leased:
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            dispatch_items=[],
            last_event_id=last_event_id,
        )

    dispatch_items: list[DispatchItem] = []
    for candidate, work in leased:
        candidate_step = candidate.plan_step
        target = candidate_step.resolved_target()
        target_node_id = node_id_for_target(target)
        action_id = ActionId(new_id("action"))

        hypothesis_id: HypothesisId | None = None
        if candidate_step.hypothesis is not None:
            assert work.hypothesis_id is not None
            hypothesis = Hypothesis(
                hypothesis_id=str(work.hypothesis_id),
                description=candidate_step.hypothesis.description,
                confidence=candidate_step.hypothesis.confidence,
                status="open",
                metadata={
                    "agent_id": str(agent.agent_id),
                    "step_id": candidate_step.step_id,
                    "target": target_display_name(target),
                    "target_node_id": str(target_node_id),
                    "violation_if_confirmed": (
                        candidate_step.hypothesis.violation_if_confirmed
                    ),
                    "severity": (
                        candidate_step.hypothesis.severity.value
                        if candidate_step.hypothesis.severity is not None
                        else None
                    ),
                    "expectation": candidate_step.expectation.model_dump(mode="json")
                    if candidate_step.expectation
                    else None,
                    **candidate_step.hypothesis.metadata,
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
            capability=candidate_step.capability,
            operation=candidate_step.operation,
            target=target,
            arguments=candidate_step.arguments,
            reason=candidate_step.reason,
            hypothesis_id=hypothesis_id,
            expected_observation=candidate_step.expected_observation,
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
                "step_id": candidate_step.step_id,
                "candidate_key": candidate.candidate_key,
                "work_id": work.work_id,
                "priority": candidate.priority.total,
                "priority_components": candidate.priority.components,
                "candidates": len(candidates),
                "batch_size": len(leased),
                "context_profile": (
                    definition.context_profile.profile_id if definition else "default"
                ),
                "projected_nodes": sum(planning_observation.node_counts.values()),
                "source_nodes": sum(observation.node_counts.values()),
            },
            correlation_id=str(action_id),
            causation_id=last_event_id,
        )
        last_event_id = proposed.event_id
        dispatch_items.append(
            DispatchItem(
                work_id=work.work_id,
                plan_step=candidate_step,
                proposal=proposal,
                last_event_id=proposed.event_id,
            )
        )

    first = dispatch_items[0]
    use_fan_out = len(dispatch_items) > 1

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.POLICY,
                "pending_action_id": first.proposal.action_id,
                "active_hypothesis_id": first.proposal.hypothesis_id,
            }
        ),
        last_event_id=last_event_id,
        plan_step=first.plan_step,
        action_proposal=first.proposal,
        current_work_id=first.work_id,
        dispatch_items=dispatch_items if use_fan_out else [],
    )
