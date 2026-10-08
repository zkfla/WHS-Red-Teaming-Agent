"""Opt-in P2 model-only fan-out; all domain effects are applied serially.

The existing coordinator owns reservations. An interrupted reservation stays
charged at its full ceiling: a crash must never mint a fresh model allowance.
No target/tool execution, path writes or evidence interpretation runs in the
parallel phase. The normal delegated path applies each admitted proposal.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from redteam.attack_graph.observation import build_observation
from redteam.attack_graph.service import AttackGraphService
from redteam.core.ids import GraphNodeId, new_id
from redteam.orchestration.agents.role_view import tools_for_role
from redteam.orchestration.cognition import DecisionBudget, DecisionFailure
from redteam.orchestration.coordination import content_hash, coordinator_for
from redteam.orchestration.delegation import DelegationState
from redteam.orchestration.delegation_decision import mark_finished
from redteam.orchestration.diagnostics import DecisionDiagnostic, diagnostic_for_exception


@dataclass
class PreparedTurn:
    state: DelegationState
    payload: dict[str, Any]
    schema: dict[str, Any]
    budget: DecisionBudget
    stamp: str
    reservation_id: str = ""
    response: dict[str, Any] | None = None
    failure: DecisionFailure | None = None
    event_id: Any = None
    requests_used: int = 0
    tokens_used: int = 0
    usage_unknown: int = 0


def _coordinator(engine):
    return engine.deps.coordinator or coordinator_for(
        engine.deps.task_graph_store, engine.deps.path_store
    )


async def reserve(engine, runtime, requested: int) -> tuple[str, int]:
    """Atomically reserve from one per-Run specialist request pool."""
    limit = engine.deps.settings.specialist_decision_pool_limit
    if limit is None:
        raise ValueError("parallel_specialist_reasoning_requires_finite_pool")
    identity = str(new_id("decision"))

    async def claim(change):
        if runtime.mission_id is not None:
            change.admit(str(runtime.run_id), str(runtime.mission_id))
        else:
            change.legacy_scope(str(runtime.run_id))
        key = ("specialist_reasoning_pool", str(runtime.run_id))
        previous = change.records.get(key)
        if previous is not None and (
            previous["limit"] != limit
            or previous["engagement_id"] != str(runtime.engagement_id)
        ):
            raise ValueError("specialist_reasoning_pool_binding_changed")
        pool = deepcopy(previous) if previous else {
            "limit": limit, "engagement_id": str(runtime.engagement_id),
            "spent": 0, "reservations": {},
        }
        remaining = max(limit - pool["spent"] - sum(pool["reservations"].values()), 0)
        allowance = min(requested, remaining)
        if allowance:
            pool["reservations"][identity] = allowance
        change.records[key] = pool
        return identity, allowance

    return await _coordinator(engine).change(claim)


async def settle(engine, runtime, identity: str, used: int) -> None:
    """Release only proven unused allowance, idempotently through the same ledger."""
    async def apply(change):
        key = ("specialist_reasoning_pool", str(runtime.run_id))
        pool = deepcopy(change.records[key])
        if pool["engagement_id"] != str(runtime.engagement_id):
            raise ValueError("specialist_reasoning_pool_binding_changed")
        ceiling = pool["reservations"].get(identity)
        if ceiling is None:
            return
        if not 0 <= used <= ceiling:
            raise ValueError("specialist_reasoning_reservation_overdrawn")
        del pool["reservations"][identity]
        pool["spent"] += used
        change.records[key] = pool

    await _coordinator(engine).change(apply)


async def subject_stamp(engine, runtime, state) -> str:
    """Relevant subjects and prerequisites only, never a whole-graph revision.

    No cached role projection is used at admission. Sibling progress outside
    these subjects cannot discard this result. Mission/operator admission is
    checked separately before model requests and again before application.
    """
    path = await engine.deps.path_store.get_active(str(runtime.run_id))
    if path is None or path.path_id != state.path_id:
        return content_hash({"missing_purpose": True})
    step = next((s for s in path.steps if s.step_id == state.path_step_id), None)
    definition = engine.deps.agents.definition(state.agent_id)
    if step is None or definition is None:
        return content_hash({"missing_purpose": True})
    service = AttackGraphService(engine.deps.graph_store)
    nodes = {}
    edges: dict[str, Any] = {}
    for ref in step.target_node_ids:
        node = await service.get_node(GraphNodeId(ref))
        if node is not None:
            nodes[str(node.id)] = node
        if definition.context_profile.neighbor_depth:
            neighbors, links = await service.subgraph_around(
                GraphNodeId(ref), radius=definition.context_profile.neighbor_depth
            )
            nodes.update((str(n.id), n) for n in neighbors)
            edges.update((str(e.id), e) for e in links)
    records = await engine.deps.evidence_store.list_for_run(runtime.run_id)
    mission = (
        await engine.deps.task_graph_store.get_mission(runtime.mission_id)
        if runtime.mission_id is not None else None
    )
    dependencies = {step.step_id, *step.depends_on}
    observation = build_observation(list(nodes.values()), edges=list(edges.values()))
    from redteam.orchestration.evidence_validity import current_validity

    validity = [await current_validity(
        engine.deps.evidence_store, service, str(runtime.run_id), str(ref), records=records,
    ) for ref in sorted(set(state.evidence_ids))]
    return content_hash({
        "path_binding": [path.run_id, path.path_id, path.version, path.mission_id],
        "current_validity": validity,
        "purpose": [s.model_dump(mode="json") for s in path.steps if s.step_id in dependencies],
        "mission_task_id": path.mission_task_id,
        "mission": mission.model_dump(mode="json") if mission is not None else None,
        "role": definition.model_dump(mode="json"),
        "catalog": [t.model_dump(mode="json") for t in tools_for_role(
            definition, engine.deps.tool_catalog
        )],
        "subjects_and_relations": engine.deps.agents.revision_fingerprint(
            state.agent_id, observation
        ),
        "sources": sorted({(str(r.source_id), r.digest) for r in records
                           if r.engagement_id == runtime.engagement_id
                           and (r.metadata.get("target_node_id") in nodes
                                or str(r.evidence_id) in state.evidence_ids)}),
        "constraints": engine.deps.engagement.model_dump(mode="json")
        if engine.deps.engagement is not None else None,
    })


def _failure(code: str, *, kind: str = "format", attempts: int = 0) -> DecisionFailure:
    summaries = {
        "specialist_decision_pool_exhausted": "The shared specialist request pool is exhausted.",
        "specialist_decision_context_changed": "Related subjects or prerequisites changed; "
        "the stale proposal was not applied. Request a bounded fresh decision.",
    }
    return DecisionFailure("worker_next_action", kind, attempts, diagnostic=DecisionDiagnostic(
        code=code, stage="worker_next_action", kind=kind, attempts=attempts,
        summary=summaries[code], retryable=False,
    ))


async def _infer(engine, runtime, turn, semaphore, role_semaphore):
    from redteam.orchestration.engine import SYSTEM

    isolated = engine.fork_decision(runtime, turn.state)
    try:
        async with semaphore, role_semaphore:
            if await subject_stamp(engine, runtime, turn.state) != turn.stamp:
                turn.failure = _failure("specialist_decision_context_changed")
                return
            turn.response = await isolated._ask(
                stage="worker_next_action", payload=deepcopy(turn.payload),
                response_schema=deepcopy(turn.schema),
                system=SYSTEM + " You own this delegated purpose. Choose methods yourself. "
                "Return evidence and gaps; only the planner can settle the overall goal.",
                budget=turn.budget,
            )
    except (DecisionFailure, ValueError, TypeError) as exc:
        turn.failure = (
            exc if isinstance(exc, DecisionFailure) else DecisionFailure(
                "worker_next_action", "format", turn.budget.used,
                diagnostic=diagnostic_for_exception(exc, stage="worker_next_action"),
            )
        )
    finally:
        value = isolated._event_runtime
        turn.requests_used = value.model_requests_used - runtime.model_requests_used
        turn.tokens_used = value.model_tokens_used - runtime.model_tokens_used
        turn.usage_unknown = value.model_usage_unknown - runtime.model_usage_unknown
        turn.event_id = isolated.last_event_id


async def decide_parallel(engine, path, runtime, context, serial):
    """Prepare once, bound requests, infer on isolated engines, apply in order."""
    turns: list[PreparedTurn] = []
    _, advanced = await serial(engine, path, runtime, context, preparing=turns)
    runtime = engine.updated_runtime or runtime
    # Bound the queued snapshot as well as in-flight model calls. Remaining
    # delegations retain their old state and are considered on a later turn.
    turns = turns[:engine.deps.settings.specialist_decision_concurrency]
    if not turns:
        return [], advanced
    semaphore = asyncio.Semaphore(engine.deps.settings.specialist_decision_concurrency)
    roles = {t.state.agent_id: asyncio.Semaphore(
        engine.deps.agents.definition(t.state.agent_id).max_concurrency
    ) for t in turns}
    for turn in turns:
        turn.reservation_id, allowance = await reserve(engine, runtime, turn.budget.max_attempts)
        if allowance == 0:
            turn.failure = _failure("specialist_decision_pool_exhausted", kind="budget")
            continue
        turn.budget = DecisionBudget.reasoning_chain(
            engine.deps.settings.decision_normal_limit,
            engine.deps.settings.decision_max_attempts, total_limit=allowance,
        )
    tasks = [asyncio.create_task(_infer(engine, runtime, t, semaphore, roles[t.state.agent_id]))
             for t in turns if t.failure is None]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Leave reservations charged when cancellation/transport effects are
        # uncertain. No orphan request is automatically replayed on restart.
        raise
    candidates = []
    for turn in turns:
        runtime.model_requests_used += turn.requests_used
        runtime.model_tokens_used += turn.tokens_used
        runtime.model_usage_unknown += turn.usage_unknown
    engine._event_runtime = engine.updated_runtime = runtime
    try:
        for turn in turns:
            engine.last_event_id = turn.event_id or engine.last_event_id
            current_path = await engine.deps.path_store.get_active(str(runtime.run_id))
            same_binding = current_path is not None and (
                current_path.run_id, current_path.path_id, current_path.version,
                current_path.mission_id, current_path.mission_task_id,
            ) == (
                path.run_id, path.path_id, path.version, path.mission_id, path.mission_task_id,
            )
            if (
                turn.failure is None
                and (not same_binding
                     or await subject_stamp(engine, runtime, turn.state) != turn.stamp)
            ):
                turn.failure = _failure(
                    "specialist_decision_context_changed", attempts=turn.budget.used
                )
            await engine._ensure_reasoning_allowed()
            # Serial completion changes stored progress, not the path version.
            # Independent replies still bind to their unchanged subjects, but
            # their completion review must use that latest canonical progress.
            # A different path/version/task cannot silently adopt an old reply;
            # real mutations during review remain guarded by the existing CAS.
            compiled, changed = await serial(
                engine, current_path if same_binding else path, runtime, context,
                prepared=turn, only=turn.state.delegation_id,
            )
            candidates.extend(compiled)
            advanced = advanced or changed
            runtime = engine.updated_runtime or runtime
            if (
                turn.failure is not None
                and turn.failure.diagnostic.code == "specialist_decision_pool_exhausted"
            ):
                # This pool is a lifetime cap, not a temporary resource wait.
                # Do not emit another zero-cost diagnostic every loop turn.
                current = runtime.delegations.get(turn.state.delegation_id)
                if current is not None:
                    runtime.delegations[current.delegation_id] = mark_finished(
                        current, reason="specialist_decision_pool_exhausted",
                    )
    finally:
        for turn in turns:
            current = runtime.delegations.get(turn.state.delegation_id)
            # A fresh gate may close an investigation without consuming its
            # already-in-flight reply. Its real request cost must still persist.
            expected = turn.state.decision_attempts_used + turn.budget.used
            if current is not None and current.decision_attempts_used < expected:
                runtime.delegations[turn.state.delegation_id] = current.model_copy(update={
                    "decision_attempts_used": expected,
                    "tokens_used": (
                        (turn.state.tokens_used or 0) + turn.budget.tokens_used
                        if turn.budget.tokens_fully_known and turn.state.tokens_used is not None
                        else None
                    ),
                })
            if turn.reservation_id:
                await settle(engine, runtime, turn.reservation_id, turn.budget.used)
        engine._event_runtime = engine.updated_runtime = runtime
    return candidates, advanced
