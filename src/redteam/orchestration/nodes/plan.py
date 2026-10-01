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
from redteam.core.ids import ActionId, GraphNodeId, HypothesisId, new_id
from redteam.orchestration.agents.base import AgentSpec, PlanningContext
from redteam.orchestration.agents.llm_provider import LLMCandidateProvider
from redteam.orchestration.agents.planners import build_planner
from redteam.orchestration.agents.providers import (
    CandidateBatch,
    CandidateDiagnostic,
    DraftDrop,
    GraphCandidateContext,
    planning_fingerprint,
)
from redteam.orchestration.candidates.catalog import ToolCatalog
from redteam.orchestration.candidates.compiler import CandidateCompiler, CandidateRejection
from redteam.orchestration.candidates.models import CandidateDraft
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
from redteam.orchestration.events import (
    AGENT_CONTEXT_PREPARED,
    AGENT_DECISION_RECORDED,
    AGENT_NO_EXECUTABLE_CANDIDATES,
    AGENT_PROVIDER_FAILED,
    AGENT_TURN_DECIDED,
    AUTHENTICATION_CHECK_DEFERRED,
    CANDIDATE_GENERATED,
    CANDIDATE_PRUNED,
    CANDIDATE_REJECTED,
    MCP_TOOL_SELECTED,
)
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.operator_intents.suppression import endpoint_excluded
from redteam.orchestration.runtime_observation import observations_for_prompt
from redteam.orchestration.scheduler import WorkScheduler
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import GraphState, RuntimeState
from redteam.orchestration.work.models import TERMINAL_STATUSES, WorkStatus


def _verification_batch_capacity(runtime: RuntimeState, settings: OrchestrationSettings) -> int:
    """Admit only actions whose worst-case retries leave the reserve intact."""
    remaining = runtime.remaining_budget
    if remaining is None:
        return settings.max_actions_per_turn
    if not settings.enforce_verification_reserve:
        return remaining
    total_budget = runtime.actions_executed + remaining
    available = max(remaining - settings.verification_reserve(total_budget), 0)
    if available <= 1 or not settings.retry_retryable_tool_failures:
        return available
    # Separate workers see independent budget mirrors. Reserve retry slots
    # before dispatching more than one; AGGREGATE cannot undo tool side effects.
    return max(1, available // (1 + settings.max_retries))


def _contains_korean(value: str | None) -> bool:
    return bool(value) and any("가" <= char <= "힣" for char in value)


def _declared_operations(agent: AgentSpec, provider: object) -> list[tuple[str, str]]:
    """Every (capability, operation) pair this agent could ever propose.

    The static playbook is the baseline. A provider that can go beyond it
    declares the extras on its declared_operations attribute -- so the
    runtime, not the provider, is the one that checks them against the
    live Tool Catalog.
    """

    pairs = {(str(step.capability), str(step.operation)) for step in agent.playbook}
    for capability, operation in getattr(provider, "declared_operations", ()) or ():
        pairs.add((str(capability), str(operation)))
    return sorted(pairs)


def _available_operations(
    catalog: ToolCatalog | None, declared: list[tuple[str, str]]
) -> tuple[tuple[tuple[str, str], ...], bool]:
    """Which declared actions the live Tool Catalog can actually execute.

    Returns (pairs, verified). With no catalog wired in, executability is
    unverifiable and the provider's declared ToolId binding remains the
    only evidence available, so verified is False and callers keep the
    existing permissive behaviour instead of silently disabling every
    tool-specific candidate.
    """

    if catalog is None:
        return (), False
    available: list[tuple[str, str]] = []
    for capability, operation in declared:
        try:
            tools = catalog.list_tools_for_action(capability, operation)
        except ValueError:
            # Noncanonical capability/operation can never be executed; the
            # compiler reports it rather than the planner guessing.
            continue
        if tools:
            available.append((capability, operation))
    return tuple(available), True


def _available_tool_ids(catalog: ToolCatalog | None, capability: str) -> tuple[str, ...]:
    """Return concrete healthy ToolIds for provider-specific candidates.

    Candidate providers may need to distinguish two tools bound to the same
    canonical action (HTTP request vs browser navigation).  This is metadata
    only; the compiler and Gateway remain the execution authorities.
    """

    if catalog is None:
        return ()
    try:
        return tuple(
            sorted(
                str(tool.tool_id)
                for tool in catalog.list_by_capability(capability)
                if getattr(tool.health_status, "value", tool.health_status) != "unhealthy"
            )
        )
    except (AttributeError, ValueError):
        return ()


def _live_action_space(
    catalog: ToolCatalog | None, capabilities: tuple[str, ...]
) -> tuple[tuple[tuple[str, str], ...], tuple[dict[str, object], ...], bool]:
    """Project the healthy live catalog into a bounded, non-secret LLM menu."""

    if catalog is None:
        return (), (), False
    operations: set[tuple[str, str]] = set()
    tools: list[dict[str, object]] = []
    seen: set[str] = set()
    for capability in capabilities:
        for descriptor in catalog.list_by_capability(capability):
            if descriptor.health_status.value == "unhealthy":
                continue
            for operation in descriptor.operations:
                operations.add((capability, str(operation)))
            tool_id = str(descriptor.tool_id)
            if tool_id in seen:
                continue
            seen.add(tool_id)
            tools.append(
                {
                    "tool_id": tool_id,
                    "server_id": descriptor.server_id,
                    "tool_name": descriptor.tool_name,
                    "capability": capability,
                    "operations": tuple(str(item) for item in descriptor.operations),
                    "target_types": tuple(descriptor.target_types),
                    "required_arguments": descriptor.required_arguments,
                    "optional_arguments": descriptor.optional_arguments,
                    "risk_level": descriptor.risk_level.value,
                    "health_status": descriptor.health_status.value,
                }
            )
    return tuple(sorted(operations)), tuple(tools), True


async def _offer_role_provider(
    provider: object, agent: AgentSpec, context: GraphCandidateContext
) -> CandidateBatch:
    """Normalize graph-aware and legacy provider shapes at one boundary."""

    graph_propose = getattr(provider, "propose_graph", None)
    legacy_propose = getattr(provider, "propose", None)
    if callable(graph_propose):
        offered = await graph_propose(agent, context)
    elif callable(legacy_propose):
        offered = await legacy_propose(
            agent, context.view, context.observation, step=context.step
        )
    else:
        raise TypeError(
            "candidate provider implements neither propose_graph(spec, context) "
            "nor propose(spec, view, observation, step=)"
        )
    return offered if isinstance(offered, CandidateBatch) else CandidateBatch(candidates=offered)


async def plan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    observation = state.get("observation")
    assert observation is not None, "plan_node runs after observe_node"

    agent = deps.agents.get(runtime.current_agent) if runtime.current_agent else None
    if agent is None:  # pragma: no cover - observe_node routes away first
        return advance(state, runtime.model_copy(update={"phase": AgentPhase.REPLAN}))
    active_task = (
        await deps.task_graph_store.get_task(runtime.active_task_id)
        if runtime.active_task_id is not None
        else None
    )
    mission = (
        await deps.task_graph_store.get_mission(runtime.mission_id)
        if runtime.mission_id is not None
        else None
    )
    operator_guidance = [
        str(item)[:500]
        for item in ((mission.operator_preferences.get("guidance") or []) if mission else [])
    ][-3:]
    runtime_observations = observations_for_prompt(runtime.recent_observations)
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
    history = await deps.work_store.list_items(run_id=runtime.run_id)
    terminal_keys = {item.candidate_key for item in history if item.status in TERMINAL_STATUSES}
    # Capability health is a liveness signal, not an authorization verdict.
    # Refresh the optional live gateway before catalog-based feasibility checks;
    # Control remains the sole authority for whether an action may run.
    refresh_health = getattr(deps.tool_gateway, "refresh_health", None)
    if callable(refresh_health):
        try:
            await refresh_health()
        except Exception:  # noqa: BLE001 - stale health cannot bypass Control
            pass
    compiler = CandidateCompiler(deps.tool_catalog)
    definition = deps.agents.definition(agent.agent_id)
    provider = (
        deps.candidate_providers.get(definition.provider_id) if definition is not None else None
    )
    drafts: list[CandidateDraft] = []
    diagnostics: list[CandidateDiagnostic] = []
    rejections: list[CandidateRejection] = []
    drops: list[DraftDrop] = []
    planning_digest = ""
    planning_source = "playbook"
    llm_turn = None
    llm_turn_error: str | None = None
    llm_candidate_order: dict[str, int] = {}
    publisher = publisher_for(state, deps, "orchestration.plan")
    last_event_id = state.get("last_event_id")
    context_published = False
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
            planning_mode = deps.settings.planning_mode
            if planning_mode in {"llm_first", "hybrid"}:
                available_operations, available_tools, operations_verified = _live_action_space(
                    deps.tool_catalog, tuple(str(item) for item in agent.capabilities)
                )
            else:
                available_operations, operations_verified = _available_operations(
                    deps.tool_catalog, _declared_operations(agent, provider)
                )
                available_tools = ()
            available_tool_ids = _available_tool_ids(deps.tool_catalog, "web")
            planning_digest = planning_fingerprint(
                observation=observation,
                available_operations=available_operations,
                available_tool_ids=available_tool_ids,
                prior_candidate_keys=terminal_keys,
            )
            llm_generation = planning_mode != "playbook_only" and deps.llm_client is not None
            generation_excerpts: tuple[dict[str, object], ...] = ()
            if llm_generation:
                observation_targets = [
                    GraphNodeId(node_id)
                    for node_ids in observation.node_ids_by_type.values()
                    for node_id in node_ids
                ]
                generation_excerpts = tuple(
                    excerpt.model_dump(mode="json")
                    for excerpt in project_evidence_excerpts(
                        await deps.evidence_store.list_for_run(runtime.run_id),
                        target_node_ids=observation_targets,
                        limit=deps.settings.planner_rerank_top_k,
                    )
                )
            context = GraphCandidateContext(
                view=service.view(),
                observation=observation,
                step=runtime.step_count,
                task_id=str(active_task.task_id) if active_task else None,
                task_source=active_task.provenance.source if active_task else None,
                task_objective=active_task.objective if active_task else None,
                mission_objective=(mission.objective if mission else runtime.current_goal),
                task_target_node_ids=(
                    tuple(active_task.related_attack_graph_node_ids) if active_task else ()
                ),
                available_capabilities=tuple(str(item) for item in agent.capabilities),
                open_hypotheses=tuple(observation.open_hypotheses),
                prior_candidate_keys=frozenset(terminal_keys),
                available_operations=available_operations,
                available_tool_ids=available_tool_ids,
                available_tools=available_tools,
                operations_verified=operations_verified,
                planning_fingerprint=planning_digest,
                evidence_excerpts=generation_excerpts,
                runtime_observations=tuple(runtime_observations),
                operator_guidance=tuple(operator_guidance),
            )

            if planning_mode == "llm_first" and llm_generation:
                # Publish the observation before the model's answer so the
                # human trace follows the same order as the actual ReAct turn.
                prepared = await publisher.publish(
                    AGENT_CONTEXT_PREPARED,
                    {
                        "agent_id": agent.agent_id,
                        "task_id": active_task.task_id if active_task else None,
                        "goal": mission.objective if mission else runtime.current_goal,
                        "graph_summary": observation.summary_line(),
                        "candidate_count": 0,
                        "action_space_count": len(available_operations),
                        "evidence_count": len(generation_excerpts),
                        "runtime_observation_count": len(runtime_observations),
                        "runtime_observation_kinds": [
                            str(item.get("kind", "runtime"))
                            for item in runtime_observations[-8:]
                        ],
                        "operator_guidance": operator_guidance,
                        "planning_mode": planning_mode,
                        "candidate_source": "llm_turn",
                    },
                    causation_id=last_event_id,
                )
                last_event_id = prepared.event_id
                context_published = True

            if planning_mode == "llm_first" and llm_generation:
                # The model gets the actual observation + live tool space and
                # owns the first proposal. Recipes are recovery knowledge, not
                # a menu that constrains the model's initial choice.
                generator = LLMCandidateProvider(deps.llm_client)
                llm_batch = await generator.propose_graph(agent, context)
                llm_turn = generator.last_decision
                llm_turn_error = generator.last_error
                drafts = list(llm_batch.candidates)
                drops = list(llm_batch.drops)
                compiled, rejections = compiler.compile_drafts_with_rejections(drafts)
                for index, draft in enumerate(llm_batch.candidates):
                    candidate = next((item for item in compiled if item.draft == draft), None)
                    if candidate is not None:
                        llm_candidate_order[candidate.candidate_key] = index
                fallback_needed = generator.last_error is not None or (
                    llm_turn is not None and llm_turn.intent == "act" and not compiled
                )
                if fallback_needed:
                    fallback = await _offer_role_provider(provider, agent, context)
                    fallback_compiled, fallback_rejections = (
                        compiler.compile_drafts_with_rejections(fallback.candidates)
                    )
                    drafts.extend(fallback.candidates)
                    diagnostics.extend(fallback.diagnostics)
                    drops.extend(fallback.drops)
                    rejections.extend(fallback_rejections)
                    compiled.extend(fallback_compiled)
                    planning_source = "recipe_fallback"
                else:
                    planning_source = "llm_turn"
            else:
                offered = await _offer_role_provider(provider, agent, context)
                drafts = list(offered.candidates)
                diagnostics = list(offered.diagnostics)
                drops = list(offered.drops)
                if planning_mode == "hybrid" and llm_generation:
                    generator = LLMCandidateProvider(deps.llm_client)
                    llm_batch = await generator.propose_graph(agent, context)
                    llm_turn = generator.last_decision
                    llm_turn_error = generator.last_error
                    covered = {
                        (str(target), draft.plan_step.operation_key)
                        for draft in drafts
                        for target in draft.target_node_ids
                    }
                    for generated_draft in llm_batch.candidates:
                        key = (
                            str(generated_draft.target_node_ids[0]),
                            generated_draft.plan_step.operation_key,
                        )
                        if key in covered:
                            drops.append(
                                DraftDrop(
                                    step_id=generated_draft.plan_step.step_id,
                                    target_node_id=str(generated_draft.target_node_ids[0]),
                                    reason="covered_by_playbook",
                                )
                            )
                        else:
                            drafts.append(generated_draft)
                    drops.extend(llm_batch.drops)
                    planning_source = "hybrid"
                compiled, rejections = compiler.compile_drafts_with_rejections(drafts)
            for draft in drafts:
                check = draft.plan_step.authentication_check
                if check is not None and not any(item.draft == draft for item in compiled):
                    diagnostics.append(
                        CandidateDiagnostic(
                            target_node_id=draft.target_node_ids[0],
                            reason="no_compatible_authentication_tool",
                            evidence_id=str(check.baseline_evidence_id),
                        )
                    )
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
    if active_task is not None:
        if active_task.related_attack_graph_node_ids:
            targets = set(active_task.related_attack_graph_node_ids)
            compiled = [
                item for item in compiled if targets.intersection(item.draft.target_node_ids)
            ]
    if llm_turn is not None or llm_turn_error is not None:
        turn_event = await publisher.publish(
            AGENT_TURN_DECIDED,
            {
                "agent_id": agent.agent_id,
                "intent": llm_turn.intent if llm_turn is not None else "act",
                "response": (
                    llm_turn.response
                    if llm_turn is not None
                    else "LLM 결정을 받을 수 없어 검증된 fallback recipe로 계속합니다."
                ),
                "decision_summary": (
                    llm_turn.decision_summary
                    if llm_turn is not None
                    else (
                        "모델 호출 실패는 실행 권한을 넓히지 않으며, "
                        "동일한 Control 검사를 거칩니다."
                    )
                ),
                "assumptions": llm_turn.assumptions if llm_turn is not None else [],
                "proposed_action_count": len(llm_turn.actions) if llm_turn is not None else 0,
                "planning_mode": deps.settings.planning_mode,
                "candidate_source": planning_source,
                "fallback_reason": "llm_call_failed" if llm_turn_error is not None else None,
            },
            causation_id=last_event_id,
        )
        last_event_id = turn_event.event_id
    if runtime.mission_id is not None:
        excluded = (
            list(mission.operator_preferences.get("excluded_endpoints") or [])
            if mission is not None
            else []
        )
        excluded_areas = (
            set(mission.operator_preferences.get("excluded_areas") or [])
            if mission is not None
            else set()
        )
        excluded_task_ids = (
            set(mission.operator_preferences.get("excluded_task_ids") or [])
            if mission is not None
            else set()
        )
        if excluded or excluded_areas or excluded_task_ids:
            allowed = []
            for candidate in compiled:
                endpoint_blocked = any(
                    endpoint_excluded(node_id, excluded)
                    for node_id in candidate.draft.target_node_ids
                )
                capability = candidate.plan_step.capability
                area_blocked = bool(
                    (capability == "web" and excluded_areas.intersection({"web", "api"}))
                    or (capability == "recon" and "reconnaissance" in excluded_areas)
                    or (capability == "mobile" and "mobile" in excluded_areas)
                    or (
                        "authorization" in excluded_areas
                        and active_task is not None
                        and "authorization" in active_task.objective.lower()
                    )
                    or (
                        "authentication" in excluded_areas
                        and active_task is not None
                        and "authentication" in active_task.objective.lower()
                    )
                )
                task_blocked = (
                    active_task is not None and str(active_task.task_id) in excluded_task_ids
                )
                if endpoint_blocked or area_blocked or task_blocked:
                    rejected = await publisher.publish(
                        CANDIDATE_REJECTED,
                        {
                            "agent_id": agent.agent_id,
                            "target_node_id": str(candidate.draft.target_node_ids[0]),
                            "capability": candidate.plan_step.capability,
                            "operation": candidate.plan_step.operation,
                            "tool_id": str(candidate.tool.tool_id),
                            "reason": (
                                "operator_excluded_endpoint"
                                if endpoint_blocked
                                else "operator_excluded_area_or_task"
                            ),
                            "field": "",
                            "candidate_key": candidate.candidate_key,
                        },
                        causation_id=last_event_id,
                    )
                    last_event_id = rejected.event_id
                else:
                    allowed.append(candidate)
            compiled = allowed
    for drop in drops:
        # A playbook entry that matched real graph context but could not be
        # turned into a draft (an argument the bound target cannot supply)
        # never reaches the compiler, so it has no CandidateRejection. Report
        # it through the same event instead of dropping it silently.
        dropped = await publisher.publish(
            CANDIDATE_REJECTED,
            {
                "agent_id": agent.agent_id,
                "target_node_id": drop.target_node_id,
                "capability": "",
                "operation": drop.step_id,
                "tool_id": "",
                "reason": drop.reason,
                "field": drop.field,
                "candidate_key": "",
            },
            causation_id=last_event_id,
        )
        last_event_id = dropped.event_id
    for rejection in rejections:
        rejected = await publisher.publish(
            CANDIDATE_REJECTED,
            {
                "agent_id": agent.agent_id,
                "target_node_id": rejection.target_node_id,
                "capability": rejection.capability,
                "operation": rejection.operation,
                "tool_id": rejection.tool_id,
                "reason": rejection.reason,
                "field": rejection.field,
                "candidate_key": rejection.candidate_key,
            },
            causation_id=last_event_id,
        )
        last_event_id = rejected.event_id
    for diagnostic in diagnostics:
        deferred = await publisher.publish(
            AUTHENTICATION_CHECK_DEFERRED,
            {
                "agent_id": agent.agent_id,
                "target_node_id": diagnostic.target_node_id,
                "reason": diagnostic.reason,
                "evidence_id": diagnostic.evidence_id,
            },
            causation_id=last_event_id,
        )
        last_event_id = deferred.event_id
    terminal_rejections: list[CandidateRejection] = []
    retained = []
    for candidate in compiled:
        if candidate.plan_step.repeatable or candidate.candidate_key not in terminal_keys:
            retained.append(candidate)
            continue
        rejection = CandidateRejection(
            target_node_id=(
                str(candidate.draft.target_node_ids[0]) if candidate.draft.target_node_ids else ""
            ),
            capability=candidate.plan_step.capability,
            operation=candidate.plan_step.operation,
            tool_id=str(candidate.tool.tool_id),
            reason="prior_terminal_execution",
            candidate_key=candidate.candidate_key,
        )
        terminal_rejections.append(rejection)
        rejected = await publisher.publish(
            CANDIDATE_REJECTED,
            {
                "agent_id": agent.agent_id,
                "target_node_id": rejection.target_node_id,
                "capability": rejection.capability,
                "operation": rejection.operation,
                "tool_id": rejection.tool_id,
                "reason": rejection.reason,
                "field": "",
                "candidate_key": rejection.candidate_key,
            },
            causation_id=last_event_id,
        )
        last_event_id = rejected.event_id
    compiled = retained
    if compiled:
        generated = await publisher.publish(
            CANDIDATE_GENERATED,
            {
                "agent_id": agent.agent_id,
                "candidate_count": len(compiled),
                "candidate_keys": [item.candidate_key for item in compiled],
                "hypothesis_ids": sorted(
                    {
                        str(item.plan_step.existing_hypothesis_id)
                        for item in compiled
                        if item.plan_step.existing_hypothesis_id is not None
                    }
                ),
                "target_node_ids": sorted(
                    {str(node_id) for item in compiled for node_id in item.draft.target_node_ids}
                ),
            },
            causation_id=last_event_id,
        )
        last_event_id = generated.event_id

    if not compiled and (drafts or drops or diagnostics):
        # The agent was activated and its provider produced *something*:
        # drafts to try, a step that matched graph context but could not
        # produce a draft at all (a drop), or a provider diagnostic. None of
        # it reached the executable frontier, so the reason it produced
        # nothing is worth saying out loud.
        #
        # Distinct from "nothing to do" at all -- no drafts, no drops and no
        # diagnostics -- which stays silent exactly as before.
        #
        # The counts are reported per source so none of them has to be read
        # as a total: total_drafts counts CandidateDrafts only (a drop is not
        # a draft), dropped_count counts steps that produced no draft, and
        # rejected_count is the compiler/terminal/drop rejection total.
        # Provider diagnostics are deliberately NOT part of rejected_count:
        # they are counted separately as provider_diagnostic_count, because a
        # diagnostic explains why nothing was offered rather than rejecting
        # something that was.
        no_candidates = await publisher.publish(
            AGENT_NO_EXECUTABLE_CANDIDATES,
            {
                "agent_id": agent.agent_id,
                "drafts_offered": len(drafts),
                "total_drafts": len(drafts),
                "dropped_count": len(drops),
                "rejected_count": len(rejections) + len(terminal_rejections) + len(drops),
                "provider_diagnostic_count": len(diagnostics),
                "rejections": [
                    {
                        "candidate_key": item.candidate_key,
                        "operation": item.operation,
                        "reason": item.reason,
                        "field": item.field,
                    }
                    for item in [*rejections, *terminal_rejections]
                ],
                "rejection_reason_summary": dict(
                    Counter(
                        [item.reason for item in rejections]
                        + [item.reason for item in terminal_rejections]
                        + [item.reason for item in drops]
                    )
                ),
                "provider_diagnostic_summary": dict(Counter(item.reason for item in diagnostics)),
            },
            causation_id=last_event_id,
        )
        last_event_id = no_candidates.event_id
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
    if planning_source == "llm_turn":
        compiled.sort(
            key=lambda item: llm_candidate_order.get(item.candidate_key, len(compiled))
        )
    else:
        compiled.sort(key=lambda item: (-item.priority.total, item.candidate_key))
    planning_observation = observation
    if definition is not None and compiled:
        # Bound only after dedup and pruning, otherwise already-terminal or
        # pruned high-ranked candidates could hide untouched lower candidates.
        compiled = compiled[: definition.context_profile.max_candidates]
        planning_observation = await ContextProjector(service, deps.context_cache).project(
            definition.context_profile,
            step=runtime.step_count,
            target_node_ids=[
                node_id for candidate in compiled for node_id in candidate.draft.target_node_ids
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
                node_id for candidate in compiled for node_id in candidate.draft.target_node_ids
            ],
            limit=evidence_limit,
        )

    if not context_published:
        context_event = await publisher.publish(
            AGENT_CONTEXT_PREPARED,
            {
                "agent_id": agent.agent_id,
                "task_id": active_task.task_id if active_task else None,
                "goal": (
                    active_task.objective
                    if active_task
                    else (mission.objective if mission else runtime.current_goal)
                ),
                "graph_summary": planning_observation.summary_line(),
                "candidate_count": len(candidates),
                "evidence_count": len(evidence_excerpts),
                "runtime_observation_count": len(runtime_observations),
                "runtime_observation_kinds": [
                    str(item.get("kind", "runtime")) for item in runtime_observations[-8:]
                ],
                "operator_guidance": operator_guidance,
                "planning_mode": deps.settings.planning_mode,
                "candidate_source": planning_source,
            },
            causation_id=last_event_id,
        )
        last_event_id = context_event.event_id

    planner = None
    selected = None
    if planning_source == "llm_turn" and compiled:
        # Preserve the model's explicit action order. Do not spend a second LLM
        # call reranking the decision that was just shown to the operator.
        selected = min(
            compiled,
            key=lambda item: llm_candidate_order.get(item.candidate_key, len(compiled)),
        )
        step = selected.plan_step
    else:
        planner = build_planner(
            agent.planner_kind,
            deps.llm_client,
            score_margin=deps.settings.planner_score_margin,
            top_k=deps.settings.planner_rerank_top_k,
        )
        step = await planner.plan(
            PlanningContext(
                agent=agent,
                observation=planning_observation,
                candidates=candidates,
                run_id=runtime.run_id,
                engagement_id=runtime.engagement_id,
                step=runtime.step_count,
                llm=deps.llm_client,
                goal=(
                    active_task.objective
                    if active_task
                    else (mission.objective if mission else runtime.current_goal)
                ),
                metadata={
                    "context_profile": (
                        definition.context_profile.profile_id if definition else "default"
                    ),
                    "projected_nodes": sum(planning_observation.node_counts.values()),
                    "source_nodes": sum(observation.node_counts.values()),
                    "evidence_excerpts": [
                        item.model_dump(mode="json") for item in evidence_excerpts
                    ],
                    "runtime_observations": runtime_observations,
                    "operator_guidance": operator_guidance,
                    "candidate_keys": [candidate.candidate_key for candidate in compiled],
                    "candidate_scores": {
                        candidate.candidate_key: candidate.priority.total
                        for candidate in compiled
                    },
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

    selected = selected or next((item for item in compiled if item.plan_step == step), None)
    if selected is None:  # Defensive: a planner may only return an offered step.
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            last_event_id=last_event_id,
        )

    selection_basis = (
        "llm_turn"
        if planning_source == "llm_turn"
        else getattr(planner, "last_decision", None)
        or ("llm" if agent.planner_kind == "llm" else "playbook_order")
    )
    korean_fallback_rationale = None
    if planning_source == "recipe_fallback" and _contains_korean(
        mission.objective if mission else runtime.current_goal
    ):
        korean_fallback_rationale = (
            "LLM 호출 결과를 사용할 수 없어 검증된 fallback recipe를 선택했습니다."
        )
    decision_rationale = (
        llm_turn.decision_summary
        if planning_source == "llm_turn" and llm_turn is not None
        else korean_fallback_rationale
        or getattr(planner, "last_rationale", None)
        or step.reason
    )
    decision_event = await publisher.publish(
        AGENT_DECISION_RECORDED,
        {
            "agent_id": agent.agent_id,
            "task_id": active_task.task_id if active_task else None,
            "selection_basis": selection_basis,
            "candidate_count": len(candidates),
            "capability": step.capability,
            "operation": step.operation,
            "target": target_display_name(step.resolved_target()),
            "rationale": str(decision_rationale)[:500],
        },
        causation_id=last_event_id,
    )
    last_event_id = decision_event.event_id

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
        batch_limit = min(batch_limit, _verification_batch_capacity(runtime, deps.settings))
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
        candidate_step = candidate.plan_step.model_copy(
            update={"candidate_key": candidate.candidate_key}
        )
        target = candidate_step.resolved_target()
        target_node_id = node_id_for_target(target)
        action_id = ActionId(new_id("action"))

        hypothesis_id: HypothesisId | None = None
        if candidate_step.existing_hypothesis_id is not None:
            assert work.hypothesis_id == candidate_step.existing_hypothesis_id
            existing = await service.get_hypothesis(str(work.hypothesis_id))
            if existing is None:
                await deps.work_store.complete(
                    work.work_id,
                    owner=str(runtime.run_id),
                    status=WorkStatus.BLOCKED,
                    now=now,
                    reason="referenced_hypothesis_missing",
                )
                continue
            hypothesis_id = work.hypothesis_id
        elif candidate_step.hypothesis is not None:
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
                    "violation_if_confirmed": (candidate_step.hypothesis.violation_if_confirmed),
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
        tool_selected = await publisher.publish(
            MCP_TOOL_SELECTED,
            {
                "action_id": proposal.action_id,
                "tool_id": str(candidate.tool.tool_id),
                "server_id": candidate.tool.server_id,
                "tool_name": candidate.tool.tool_name,
                "capability": proposal.capability,
                "operation": proposal.operation,
                "risk_level": candidate.tool.risk_level.value,
                "health_status": candidate.tool.health_status.value,
            },
            correlation_id=str(action_id),
            causation_id=last_event_id,
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
                "hypothesis_id": hypothesis_id,
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
                "reason": candidate_step.reason,
                "selection_basis": selection_basis,
            },
            correlation_id=str(action_id),
            causation_id=tool_selected.event_id,
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

    if not dispatch_items:
        return advance(
            state,
            runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
            plan_step=None,
            action_proposal=None,
            current_work_id=None,
            dispatch_items=[],
            last_event_id=last_event_id,
        )

    first = dispatch_items[0]
    use_fan_out = len(dispatch_items) > 1
    deps.batch_pending_approvals.discard(runtime.run_id)

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
