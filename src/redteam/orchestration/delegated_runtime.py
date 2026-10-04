"""Bind specialist decisions to the existing per-action execution loop."""

from __future__ import annotations

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ids import GraphNodeId
from redteam.orchestration.cognition import DecisionBudget, DecisionFailure
from redteam.orchestration.context_projection import ContextProjector, project_evidence_excerpts
from redteam.orchestration.delegation import DelegationRequest
from redteam.orchestration.delegation_decision import (
    TERMINATION_WORKER_HELD,
    DelegationGateInput,
    TurnConsumption,
    apply_turn_result,
    build_worker_next_action_payload,
    close_from_gate,
    gate_delegation_decision,
    mark_active,
    mark_finished,
    open_delegation,
    parse_worker_next_action,
    remaining_decisions,
    worker_next_action_schema,
)
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.knowledge_validation import assessment_blocked, check_ready_step
from redteam.orchestration.paths.models import step_definition_equal
from redteam.orchestration.reasoning_time import reasoning_time_allowed
from redteam.orchestration.target_references import register_references
from redteam.orchestration.work.models import WorkBudget, WorkStatus


def _publish_runtime(engine, runtime, delegations):
    updated = runtime.model_copy(update={"delegations": delegations})
    engine.updated_runtime = updated
    engine._event_runtime = updated
    return updated


async def _worker_hold_fingerprint(engine, runtime, state):
    """Content visible to this purpose; new record ids alone never wake it."""
    from redteam.core.enums import EvidenceSourceType
    from redteam.orchestration.completion import check_record_linkage
    from redteam.orchestration.evidence_access import raw_from_metadata
    from redteam.orchestration.progress import knowledge_fingerprint
    from redteam.orchestration.target_references import resolve_reference

    try:
        service = AttackGraphService(engine.deps.graph_store)
        observation = await service.observe(step=runtime.step_count)
        definition = engine.deps.agents.definition(state.agent_id)
        path = await engine.deps.path_store.get_active(str(runtime.run_id))
        step = (
            next((item for item in path.steps if item.step_id == state.path_step_id), None)
            if path
            else None
        )
        if definition is None or step is None:
            return None
        projected = await ContextProjector(service, engine.deps.context_cache).project(
            definition.context_profile,
            step=runtime.step_count,
            target_node_ids=[GraphNodeId(item) for item in step.target_node_ids],
            fallback=observation,
            cache_namespace=str(runtime.run_id),
        )
        relevant = {item for ids in projected.node_ids_by_type.values() for item in ids}
        relevant.update(step.target_node_ids)
        records = {
            str(item.evidence_id): item
            for item in await engine.deps.evidence_store.list_for_run(runtime.run_id)
            if item.run_id == runtime.run_id and item.engagement_id == runtime.engagement_id
        }
        content = []
        for identity, record in records.items():
            raw = raw_from_metadata(record.metadata)
            if record.source_type is EvidenceSourceType.TOOL and (
                record.metadata.get("target_node_id") in relevant
                and isinstance(raw, dict)
                and not check_record_linkage(
                    record, record.metadata, raw, identity, require_proposal=True
                )
            ):
                content.append(
                    {
                        "target": record.metadata.get("target_node_id"),
                        "tool": record.source_id,
                        "success": raw.get("success"),
                        "output": raw.get("output"),
                        "error": raw.get("error"),
                    }
                )
            for entry in record.metadata.get("target_references", []):
                source = (
                    records.get(str(entry.get("source_evidence_id", "")))
                    if isinstance(entry, dict)
                    else None
                )
                if source is None or source.metadata.get("target_node_id") not in relevant:
                    continue
                target = resolve_reference(entry, records, str(runtime.run_id))
                if target is not None:
                    content.append({"referenced_target": target.model_dump(mode="json")})
        return knowledge_fingerprint(
            {
                "facts": [
                    {"graph": engine.deps.agents.revision_fingerprint(state.agent_id, observation)},
                    *content,
                ]
            }
        )
    except Exception:  # noqa: BLE001 - failed reads cannot wake a held investigation
        return None


async def _relink_same_purpose(engine, runtime, path, state):
    """Re-link one delegation when a newer path keeps the same purpose.

    Same investigation and mission with a newer version: the stored
    version's step definition must still mean the same purpose with the
    same actual dependencies, and the step must sit outside the active
    plan's own changed ids (which already include descendants). Anything
    else returns None so the caller finishes just that affected step.
    """

    current = next((item for item in path.steps if item.step_id == state.path_step_id), None)
    if current is None or state.path_step_id in set(path.changed_step_ids or []):
        return None
    try:
        plans = await engine.deps.path_store.list_paths(str(runtime.run_id))
    except Exception:  # noqa: BLE001 - unreadable history cannot prove sameness
        return None
    previous = next(
        (
            step
            for plan in plans
            if plan.path_id == state.path_id and plan.version == state.path_version
            for step in plan.steps
            if step.step_id == state.path_step_id
        ),
        None,
    )
    if previous is None:
        return None
    if not step_definition_equal(previous, current):
        return None
    return state.model_copy(update={"path_version": path.version})


async def accept_delegations(engine, path, requests, runtime):
    """Runtime owns identities and finite default limits, never the model."""
    from redteam.orchestration.engine import _collect_check_assessment

    material = await _collect_check_assessment(engine.deps, runtime)
    blocked = assessment_blocked(material)
    if blocked:
        raise ValueError(blocked)
    values = dict(runtime.delegations)
    for request in requests:
        step = next((item for item in path.steps if item.step_id == request.path_step_id), None)
        if step is None or check_ready_step(path, step, material):
            raise ValueError("delegation needs a ready investigation purpose")
        agent = engine.deps.agents.get(request.agent_id)
        if agent is None:
            raise ValueError("delegation needs a registered executable specialist")
        if any(
            item.path_id == path.path_id
            and item.path_version == path.version
            and item.path_step_id == step.step_id
            and item.status in {"active", "held"}
            for item in values.values()
        ):
            raise ValueError("an investigation already owns this step")
        limit = runtime.remaining_budget
        if limit is None:
            limit = engine.deps.settings.max_steps
        if limit <= 0:
            raise ValueError("there is no remaining execution allowance to delegate")
        requested = request.budget or WorkBudget(max_actions=limit)
        budget = requested.model_copy(update={"max_actions": min(requested.max_actions, limit)})
        bound = DelegationRequest(
            path_step_id=step.step_id,
            agent_id=request.agent_id,
            objective=request.objective or step.objective,
            budget=budget,
        )
        state = open_delegation(
            bound,
            path_id=path.path_id,
            path_version=path.version,
            mission_task_id=path.mission_task_id,
            decision_limit=max(engine.deps.settings.max_steps - runtime.step_count, 1),
        )
        values[state.delegation_id] = state
    return _publish_runtime(engine, runtime, values)


async def decide_delegations(engine, path, runtime, context):
    """Each active specialist proposes its next call; root receives finished results."""
    from redteam.orchestration.engine import SYSTEM, ActionIntent

    values = dict(runtime.delegations)
    candidates = []
    advanced = False
    history = await engine.deps.work_store.list_items(run_id=runtime.run_id)
    for identity, state in list(values.items()):
        if state.status == "finished":
            continue
        if state.path_id != path.path_id or state.mission_task_id != path.mission_task_id:
            values[identity] = mark_finished(state, reason="investigation_context_changed")
            advanced = True
            continue
        if state.path_version != path.version:
            # Same investigation and mission under a newer version: follow
            # the version when the step still means the same purpose with
            # the same actual dependencies; finish only affected steps.
            relinked = await _relink_same_purpose(engine, runtime, path, state)
            if relinked is None:
                values[identity] = mark_finished(state, reason="investigation_context_changed")
                advanced = True
                continue
            values[identity] = relinked
            state = relinked
        # Only a genuinely in-flight row blocks a new proposal. A parked
        # PENDING row (a sibling released by the deferred-for-approval path
        # in worker_node) is re-acquirable through plan_node's pending lookup
        # and must never stall this delegation autonomously after resume.
        if (
            state.status == "held"
            and state.termination_reason == TERMINATION_WORKER_HELD
            and runtime.pending_approval_id is None
        ):
            # Model hold wakes only on a role-visible content change, never
            # on a fresh receipt id. A missing baseline records the current
            # revision and keeps waiting; an unreadable graph keeps the hold.
            fingerprint = await _worker_hold_fingerprint(engine, runtime, state)
            baseline = state.held_fingerprint
            if fingerprint is None or baseline is None or baseline == fingerprint:
                if fingerprint is not None and baseline is None:
                    values[identity] = state.model_copy(update={"held_fingerprint": fingerprint})
                continue
            state = state.model_copy(update={"held_fingerprint": fingerprint})
            state = mark_active(state)
            values[identity] = state
        if state.status == "held" and runtime.pending_approval_id is None:
            continue
        if any(
            item.delegation_id == identity
            and item.status in (WorkStatus.LEASED, WorkStatus.RUNNING)
            for item in history
        ):
            continue
        gate = gate_delegation_decision(
            DelegationGateInput(
                delegation=state,
                global_remaining_actions=runtime.remaining_budget,
                deadline_exceeded=not reasoning_time_allowed(engine.deps.engagement, runtime),
                approval_pending=runtime.pending_approval_id is not None,
                usage_unknown=state.tokens_used is None,
            )
        )
        if not gate.investigate_model_allowed and not gate.cleanup_model_allowed:
            values[identity], _ = close_from_gate(state, gate)
            advanced = True
            continue
        definition = engine.deps.agents.definition(state.agent_id)
        role = engine.deps.agents.get(state.agent_id)
        step = next((item for item in path.steps if item.step_id == state.path_step_id), None)
        if role is None or definition is None or step is None:
            values[identity] = mark_finished(state, reason="specialist_or_purpose_unavailable")
            advanced = True
            continue
        service = AttackGraphService(engine.deps.graph_store)
        observation = await service.observe(step=runtime.step_count)
        projected = await ContextProjector(service, engine.deps.context_cache).project(
            definition.context_profile,
            step=runtime.step_count,
            target_node_ids=[GraphNodeId(item) for item in step.target_node_ids],
            fallback=observation,
            cache_namespace=str(runtime.run_id),
        )
        relevant_ids = {identity for ids in projected.node_ids_by_type.values() for identity in ids}
        relevant_ids.update(step.target_node_ids)
        records = [
            item
            for item in await engine.deps.evidence_store.list_for_run(runtime.run_id)
            if item.engagement_id == runtime.engagement_id
        ]
        relevant = [
            item.model_dump(mode="json")
            for item in project_evidence_excerpts(
                records,
                target_node_ids=[GraphNodeId(item) for item in sorted(relevant_ids)],
                limit=definition.context_profile.max_evidence,
            )
        ]
        payload = build_worker_next_action_payload(
            delegation=state,
            evidence_ids=state.evidence_ids,
            evidence_excerpts=redact_secret_material(relevant),
            unresolved=state.unresolved,
            remaining_actions=gate.remaining_actions,
        )
        knowledge = dict(context.get("knowledge") or {})
        for section in ("facts", "known_targets"):
            knowledge[section] = [
                item for item in knowledge.get(section, []) if item.get("node_id") in relevant_ids
            ]
        provided = dict(knowledge.get("provided") or {})
        provided["claims"] = [
            item for item in provided.get("claims", []) if item.get("node_id") in relevant_ids
        ]
        knowledge["provided"] = provided
        for section in ("hypotheses", "findings"):
            details = dict(knowledge.get(section) or {})
            details["items"] = [
                item
                for item in details.get("items", [])
                if item.get("target_node_id") in relevant_ids
            ]
            knowledge[section] = details
        reader = EvidenceReader(engine.deps.evidence_store)
        payload["requested_evidence"] = (
            [
                await reader.read(evidence_id=item, run_id=str(runtime.run_id), limit=4000)
                for item in state.evidence_ids[-definition.context_profile.max_evidence :]
            ]
            if definition.context_profile.max_evidence
            else []
        )
        payload.update(
            goal=context.get("goal"),
            success_criteria=context.get("success_criteria"),
            active_task=context.get("active_task"),
            specialist=definition.model_dump(mode="json"),
            engagement_constraints=context.get("engagement_constraints"),
            knowledge=knowledge,
            role_observation=projected.model_dump(mode="json"),
            tools=[
                item
                for item in context.get("tools", [])
                if set(item.get("capability_tags", [])) & set(role.capabilities)
            ],
            execution_history=[
                item
                for item in context.get("execution_history", [])
                if set(item.get("target_node_ids", [])) & set(step.target_node_ids)
                or item.get("delegation_id") == identity
            ],
        )
        if gate.cleanup_model_allowed and not gate.investigate_model_allowed:
            payload["finish_only"] = True
            payload["termination_reason"] = gate.reason
        allowance = min(
            engine.deps.settings.decision_max_attempts,
            max(remaining_decisions(state) - (1 if gate.investigate_model_allowed else 0), 1),
        )
        budget = DecisionBudget(allowance)
        schema = worker_next_action_schema()
        action_schema = engine._writing_schema(ActionIntent)
        for field in ("path_step_id", "agent_id"):
            action_schema.get("properties", {}).pop(field, None)
            action_schema["required"] = [
                name for name in action_schema["required"] if name != field
            ]
        schema["properties"]["actions"]["items"] = action_schema
        if "$defs" in action_schema:
            schema["$defs"] = action_schema.pop("$defs")
        if not gate.investigate_model_allowed:
            schema["properties"]["decision"]["enum"] = ["finish"]
        engine.decision_token_limit = (
            max(state.budget.max_tokens - (state.tokens_used or 0), 0)
            if state.budget.max_tokens is not None
            else None
        )
        verdict = None
        error = None
        engine.active_delegation = state
        try:
            raw = await engine._ask(
                stage="worker_next_action",
                payload=payload,
                response_schema=schema,
                system=SYSTEM + " You own this delegated purpose. Choose methods yourself. "
                "Return evidence and gaps; only the root can settle the overall goal.",
                budget=budget,
            )
            verdict = parse_worker_next_action(raw)
        except (DecisionFailure, ValueError, TypeError) as exc:
            error = type(exc).__name__
        finally:
            engine.decision_token_limit = None
            engine.active_delegation = None
        result = apply_turn_result(
            delegation=state,
            gate=gate,
            verdict=verdict,
            error=error,
            consumption=TurnConsumption(
                attempts=budget.used,
                tokens=budget.tokens_used if budget.tokens_fully_known else None,
                usage_unknown=not budget.tokens_fully_known,
            ),
        )
        values[identity] = result.delegation
        if verdict is not None and verdict.decision == "hold":
            fingerprint = await _worker_hold_fingerprint(engine, runtime, state)
            values[identity] = values[identity].model_copy(update={"held_fingerprint": fingerprint})
        advanced = True
        runtime = _publish_runtime(engine, runtime, values)
        if verdict is None:
            continue
        if verdict.evidence_ids and verdict.decision != "read_evidence":
            reader = EvidenceReader(engine.deps.evidence_store)
            admitted = [
                await reader.read(evidence_id=item, run_id=str(runtime.run_id), limit=0)
                for item in verdict.evidence_ids
            ]
            values[identity] = values[identity].model_copy(
                update={
                    "evidence_ids": list(
                        dict.fromkeys(
                            [
                                *values[identity].evidence_ids,
                                *(
                                    item["evidence_id"]
                                    for item in admitted
                                    if item["status"] == "ok"
                                ),
                            ]
                        )
                    )
                }
            )
        if verdict.target_references:
            await register_references(
                engine.deps.evidence_store, runtime, list(verdict.target_references)
            )
        if verdict.decision == "read_evidence":
            reader = EvidenceReader(engine.deps.evidence_store)
            excerpts = [
                await reader.read(evidence_id=item, run_id=str(runtime.run_id), limit=4000)
                for item in verdict.evidence_ids
            ]
            # Store refs; the next scoped call reads them from the original store.
            values[identity] = values[identity].model_copy(
                update={
                    "evidence_ids": list(
                        dict.fromkeys(
                            [
                                *values[identity].evidence_ids,
                                *(
                                    item["evidence_id"]
                                    for item in excerpts
                                    if item["status"] == "ok"
                                ),
                            ]
                        )
                    )
                }
            )
            continue
        if verdict.decision != "propose_actions":
            continue
        intents = [
            ActionIntent.model_validate(
                {**item, "path_step_id": state.path_step_id, "agent_id": state.agent_id}
            )
            for item in verdict.actions
        ]
        compiled, errors = await engine.compile_actions(path, intents, runtime, context)
        for item in compiled[: gate.remaining_actions]:
            step_value = item.draft.plan_step.model_copy(update={"delegation_id": identity})
            candidates.append(
                item.model_copy(
                    update={"draft": item.draft.model_copy(update={"plan_step": step_value})}
                )
            )
        if errors:
            values[identity] = values[identity].model_copy(
                update={"unresolved": redact_secret_material(errors[-10:])}
            )
    runtime = _publish_runtime(engine, runtime, values)
    return candidates, advanced


def close_delegations(runtime, reason):
    """Keep collected references and the original exit cause without inference."""
    if reason in {"approval_pending", "operator_paused"}:
        return runtime
    values = {
        identity: mark_finished(state, reason=reason)
        for identity, state in runtime.delegations.items()
    }
    return runtime.model_copy(update={"delegations": values})
