"""Task conversation inputs, separate from current execution authority.

No model/target calls or second store. Frozen displays live in the existing
instance checkpoint; canonical admission still reads the current Task/Control.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from redteam.core.contracts.tool import model_input_schema
from redteam.core.ports.llm import NativeToolSpec
from redteam.orchestration.agents.tool_access import eligible_role_tool_ids
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.engine import DecisionRuleError
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.tool_discovery import (
    NATIVE_INPUT_LIMIT,
    bootstrap_local_ids,
    detail_fetched_tool_ids,
    discovery_lookup_wired,
    program_execution_environment,
)
from redteam.orchestration.worker_session import (
    FrozenMessage,
    SessionArchive,
    SessionBinding,
    WorkerSession,
)

WORKER_CONVERSATION_VERSION = 3


async def binding_context(deps, runtime) -> dict[str, Any]:
    """Current role/Task for binding only; never rebuild the full prompt context."""
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    if task is None:
        raise DecisionRuleError("The assigned Task is missing.", code="probe_task_missing")
    roles = ([runtime.current_agent] if runtime.current_agent is not None
             else [agent.agent_id for agent in deps.agents.all()])
    selected = {str(role): sorted(await eligible_role_tool_ids(deps, str(role))) for role in roles}
    return {"active_task": task.model_dump(mode="json"), "tools_by_role": selected}


def selected_specs(deps, context):
    local = {str(item.tool_id): item for item in deps.local_tools.descriptors()}
    eligible = sorted({identity for ids in context["tools_by_role"].values() for identity in ids})
    catalog = {identity: deps.tool_catalog.get(identity) for identity in eligible}
    descriptors = {**{key: item for key, item in catalog.items() if item is not None}, **local}
    specs = {identity: NativeToolSpec(
        name=exposed_tool_name(identity), description=str(item.description or identity),
        input_schema=model_input_schema(item.input_schema or {"type": "object"}),
    ) for identity, item in descriptors.items()}
    return local, eligible, specs


def initial_tools(deps, context, *, messages, system, input_limit=NATIVE_INPUT_LIMIT):
    local, eligible, specs = selected_specs(deps, context)
    if discovery_lookup_wired(local):
        base = set(bootstrap_local_ids(list(local.values())))
        base.update(identity for identity, item in local.items() if item.initial_exposure)
    else:
        base = set(local)
    tools = [specs[identity] for identity in sorted(base)]
    catalog = [specs[identity] for identity in eligible if identity not in base]
    # Only the INITIAL selection may choose full catalog vs lazy discovery.
    # Subsequent requests never evict previously exposed schemas to make space.
    if not discovery_lookup_wired(local) or input_limit.fits(
        messages=messages, tools=[*tools, *catalog], system=system,
    ):
        tools.extend(catalog)
    return tools


def provider_binding(deps):
    return {"components": deps.component_binding.model_dump(mode="json")
            if deps.component_binding else None,
            "worker_conversation_version": WORKER_CONVERSATION_VERSION}


def checkpoint_session(state, session):
    state["worker_session"] = session.checkpoint()
    # The frozen transcript has ONE owner. This is only a legacy checkpoint
    # channel; keeping a second copy here doubles storage and invites divergence.
    state["native_messages"] = []


def restore_session(state, *, deps, runtime, task, system, composed_system,
                    allow_purpose_change=False, allow_closed=False):
    raw = state["worker_session"]
    session = WorkerSession.model_validate_json(json.dumps(raw, allow_nan=False))
    if session.system is None or not session.messages:
        raise DecisionRuleError("The session has no initial display.",
                                code="worker_session_initial_display_missing")
    if session.system != system:
        raise DecisionRuleError("The code-owned session prompt changed.",
                                code="worker_session_prompt_changed")
    tools = session.tools()
    if session.initial_tool_count > len(tools):
        raise DecisionRuleError("The initial tool prefix is incomplete.",
                                code="worker_session_tool_contract_changed")
    expected = SessionBinding.capture(
        runtime, task, system=composed_system, tools=tools[:session.initial_tool_count],
        provider_binding=provider_binding(deps),
    )
    purpose_only = session.binding.model_copy(update={"purpose_hash": expected.purpose_hash})
    if session.binding != expected and not (allow_purpose_change and purpose_only == expected):
        raise DecisionRuleError("The saved conversation binding does not match this Task.",
                                code="worker_session_binding_mismatch")
    if session.status == "closed" and not allow_closed:
        raise DecisionRuleError("The task conversation is closed.", code="worker_session_closed")
    # Only the existing runner/Control path can resume an executing actor.
    # Waiting here is conversation metadata; it never grants admission.
    return session.model_copy(update={"status": "open", "wait_reason": None})


async def validate_checkpoint_session(deps, runtime, values):
    """Validate BEFORE original-call reconciliation or tools-first resume."""
    if values.get("worker_session") is None:
        if values.get("native_messages") or values.get("native_calls"):
            raise DecisionRuleError("Legacy history cannot resume as a new conversation.",
                                    code="worker_session_legacy_checkpoint")
        return
    # Lazy import avoids coupling the session contract to graph node assembly.
    from redteam.orchestration.nodes.react import SYSTEM
    from redteam.orchestration.probes import PROBE_SYSTEM

    session = WorkerSession.model_validate_json(json.dumps(values["worker_session"]))
    task = await deps.central_state.get_task(session.binding.task_id)
    if task is None:
        raise DecisionRuleError("The session Task is missing.", code="probe_task_missing")
    system = SYSTEM + PROBE_SYSTEM if task.probe_limits else SYSTEM
    composed = (deps.agent_prompts.compose("native_action", system,
                                          role_id=(str(runtime.current_agent)
                                                   if runtime.current_agent is not None else None))
                if deps.agent_prompts else system)
    restore_session(values, deps=deps, runtime=runtime, task=task, system=system,
                    composed_system=composed, allow_purpose_change=not values.get("native_calls"),
                    allow_closed=True)
    validate_pairing(session.conversation(), pending_ids=[
        call.call_id for call in values.get("native_calls", [])
    ])
    _, _, specs = selected_specs(deps, await binding_context(deps, runtime))
    validate_tool_contract(session, specs)


def validate_tool_contract(session, specs):
    current = {spec.name: spec for spec in specs.values()}
    if any(tool.name in current and tool != current[tool.name] for tool in session.tools()):
        raise DecisionRuleError("An exposed tool contract changed; retain the original session.",
                                code="worker_session_tool_contract_changed")


def validate_pairing(messages, *, pending_ids=()):
    """A call/result ID is a protocol identity, not a new application identity."""
    outstanding: set[str] = set()
    for message in messages:
        if message["role"] == "assistant":
            if outstanding:
                raise DecisionRuleError("Missing original tool results.",
                                        code="worker_session_tool_pairing_invalid")
            identities = [call["id"] for call in message.get("tool_calls") or []]
            if len(set(identities)) != len(identities):
                raise DecisionRuleError("Duplicate call IDs in one response.",
                                        code="worker_session_tool_pairing_invalid")
            outstanding.update(identities)
        elif message["role"] == "tool":
            identity = message.get("tool_call_id")
            if identity not in outstanding:
                raise DecisionRuleError("Unexpected or duplicate tool result.",
                                        code="worker_session_tool_pairing_invalid")
            outstanding.remove(identity)
    if outstanding != set(pending_ids):
        raise DecisionRuleError("Checkpoint call/result pairing does not match pending calls.",
                                code="worker_session_tool_pairing_invalid")


def original_purpose_task(values, submission, current):
    """Validate completed receipts against their recorded original purpose.

    Only a matching frozen initial Task can prove a previous purpose. Pending
    executions still require the current Task and are never grandfathered.
    """
    from redteam.core.contracts.completion import completion_contract_hash
    from redteam.core.contracts.mission import MissionTask
    from redteam.orchestration.execution_journal import checked_receipt

    expected = checked_receipt(submission)["plan_step"].get("task_purpose_hash")
    if current is None or completion_contract_hash(current) == expected:
        return current
    raw = values.get("worker_session")
    if raw is None:
        return current
    session = WorkerSession.model_validate_json(json.dumps(raw))
    generations = [*session.archives, session]
    for generation in generations:
        if generation.binding.purpose_hash != expected or not generation.messages:
            continue
        context = json.loads(generation.messages[0].message()["content"])
        original = MissionTask.model_validate(context["active_task"])
        if (original.task_id == current.task_id and original.mission_id == current.mission_id
                and completion_contract_hash(original) == expected):
            return original
    return current  # The existing receipt validator reports the unresolved mismatch.


async def initial_context(deps, runtime, engine, observation):
    """One initial display, or an explicit generation boundary, never a normal turn."""
    from redteam.orchestration.probes import context_for_probe

    context = await context_for_probe(deps, runtime, await engine.context(runtime, observation))
    for key in ("evidence_read_contract", "execution_options", "local_tools", "tools"):
        context.pop(key, None)
    context["investigation_tasks"] = [{"task_id": str(runtime.active_task_id),
                                       "see": "active_task"}]
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    if task is not None and task.source_task_id is not None:
        context["source_result"] = await source_result_context(deps, task.model_dump(mode="json"))
    return context


async def source_result_context(deps, task):
    source = await deps.central_state.get_task(task["source_task_id"])
    matched = (source is not None and str(source.mission_id) == task["mission_id"]
                   and source.result is not None
                   and source.result.result_id == task["source_result_id"])
    return {
        "task_id": task["source_task_id"], "result_id": task["source_result_id"],
        "status": "available" if matched else "source_result_unavailable_or_changed",
        "result": source.result.model_dump(mode="json") if matched else None,
        "note": "Explicit source DATA only; source conversation is not inherited. "
                "Current source/review validity still requires canonical validation.",
    }


async def compact_session(session, *, deps, runtime, engine, observation, task,
                          composed_system, input_limit, reason, pending_calls=()):
    """Reference-only display compression at a completed round, without an LLM.

    Retain public assertions/negative feedback/identifiers and all call pairs.
    Only EvidenceReader display bodies can become explicit original references.
    If that is insufficient, or opaque provider state cannot be safely replaced,
    hold instead of guessing a semantic summary or dropping execution obligations.
    """
    from redteam.orchestration.central_completion import unsafe

    messages, tools = session.conversation(), session.tools()
    validate_pairing(messages, pending_ids=[call.call_id for call in pending_calls])
    executions = await deps.central_state.list_executions(task_id=str(task.task_id))
    if pending_calls or any(unsafe(item) for item in executions) or any(
        message.get("provider_continuation") for message in messages
    ):
        raise DecisionRuleError("Pending calls or opaque provider state prevent safe compaction.",
                                code="worker_session_compaction_held", location="input")
    context = await initial_context(deps, runtime, engine, observation)
    # The paired history below already owns local replies and reader requests.
    # Do not reconstruct their bodies a second time in the new baseline. Tool
    # schemas stay in the fixed envelope; inventory remains discoverable.
    context = {key: value for key, value in context.items() if key in {
        "goal", "success_criteria", "mission", "active_task", "investigation_tasks",
        "engagement_constraints", "knowledge", "evidence_validity", "evidence", "evidence_index",
        "evidence_count", "evidence_omitted_count", "probe", "source_result", "execution_history",
        "execution_history_count", "execution_history_omitted_count", "remaining_budget",
        "operator_instructions", "feedback", "runtime_observations",
        "program_execution_environment",
    }}
    context["conversation_boundary"] = {
        "generation": session.generation + 1, "previous_generation": session.generation,
        "reason": reason, "previous_purpose_hash": session.binding.purpose_hash,
        "note": "Explicit context generation; previous exact displays remain archived in the "
                "Task checkpoint. Prior window text below is omitted, not negative evidence. "
                "Read originals by evidence ID/offset/query when needed. No claim was reviewed "
                "or settled by compression. This boundary changes the cache prefix.",
    }
    first = FrozenMessage.capture({"role": "user", "content": json.dumps(context,
                                   ensure_ascii=False)}, max_content_chars=input_limit.max_bytes)
    compacted = [first]
    for original in messages[1:]:
        message = json.loads(json.dumps(original))
        if message["role"] == "tool":
            body = json.loads(message.get("content") or "{}")
            windows = body.get("original_results", [])
            if body.get("evidence_read_request") and isinstance(body.get("payload"), dict):
                windows = [*windows, body["payload"]]
            for window in windows:
                if isinstance(window, dict) and ("text" in window or "program_output" in window):
                    window["archived_display"] = {"generation": session.generation,
                                                   "hash": content_hash(window)}
                    if "text" in window:
                        window.pop("text")
                        window["text_omitted"] = True
                    if "program_output" in window:
                        window.pop("program_output")
                        window["program_output_omitted"] = True
            message["content"] = json.dumps(body, ensure_ascii=False)
        compacted.append(FrozenMessage.capture(message,
                                              max_content_chars=input_limit.max_bytes))
    before = input_limit.size(messages=messages, tools=tools, system=composed_system)
    after = input_limit.size(messages=[message.message() for message in compacted], tools=tools,
                             system=composed_system)
    if after > input_limit.max_bytes or (reason == "input_limit" and after >= before):
        raise DecisionRuleError(f"Required context/reference pairs use {after} bytes "
                                f"(limit {input_limit.max_bytes}); no messages or tool schemas "
                                "were silently dropped.",
                                code="native_input_limit_exceeded", location="input")
    binding = SessionBinding.capture(runtime, task, system=composed_system,
                                     tools=tools[:session.initial_tool_count],
                                     provider_binding=provider_binding(deps))
    return session.model_copy(update={
        "generation": session.generation + 1, "binding": binding, "messages": tuple(compacted),
        "archives": (*session.archives, SessionArchive(
            generation=session.generation, reason=reason, binding=session.binding,
            messages=session.messages, tools_json=session.tools_json,
            input_bytes_before=before, input_bytes_after=after,
        )),
    })


async def update_session(session, *, deps, runtime, context):
    """Monotonic schema discovery and relevant DATA changes, not per-turn snapshots."""
    local, eligible, specs = selected_specs(deps, context)
    bindings = {spec.name: identity for identity, spec in specs.items()}
    fetched = detail_fetched_tool_ids(session.conversation(), bindings=bindings)
    tools = session.tools()
    exposed = {tool.name for tool in tools}
    validate_tool_contract(session, specs)
    additions = [specs[identity] for identity in sorted(set(fetched) & specs.keys())
                 if specs[identity].name not in exposed]
    if additions:
        tools.extend(additions)
        session = session.model_copy(update={
            "tools_json": json.dumps([asdict(tool) for tool in tools], ensure_ascii=False),
        })
        session = session.append(FrozenMessage.capture({"role": "user", "content": json.dumps({
            "conversation_notice": "Tool schema extension; earlier messages are unchanged. "
                                   "This changes the cache prefix, not execution permission.",
            "added_tool_names": [tool.name for tool in additions],
        })}, max_content_chars=NATIVE_INPUT_LIMIT.max_bytes))

    mission = await deps.central_state.get_mission(runtime.mission_id)
    task = context["active_task"]
    changes = {
        "mission": {"objective": mission.objective, "status": mission.status.value,
                    "operator_preferences": mission.operator_preferences,
                    "success_criteria": mission.success_criteria},
        "task_state": {key: task.get(key) for key in (
            "status", "blocking_reason", "required", "result", "source_task_id",
            "source_result_id", "source_question_id", "evidence_ids",
        )},
        "execution_environment": program_execution_environment(
            getattr(deps, "shell_programs", None), eligible,
        ),
        "tool_availability": {"eligible_ids": eligible, "local_ids": sorted(local)},
    }
    if task.get("source_task_id"):
        changes["source_result"] = await source_result_context(deps, task)
    from redteam.attack_graph.service import AttackGraphService
    from redteam.core.ports.control_plane import PolicyExplanationControl
    from redteam.orchestration.evidence_validity import current_validity

    changes["policy"] = {
        "scope": deps.engagement.scope.model_dump(mode="json"),
        "rules_of_engagement": [rule.model_dump(mode="json")
                                for rule in deps.engagement.rules_of_engagement],
        "meaning": await deps.control_plane.describe_policy(runtime.engagement_id)
        if isinstance(deps.control_plane, PolicyExplanationControl) else None,
    }
    graph = AttackGraphService(deps.graph_store)
    evidence_ids = set(task.get("evidence_ids", []))
    related = {}
    for identity, admitted in (runtime.probe_hypotheses or {}).items():
        current = await graph.get_hypothesis(identity)
        related[identity] = current.model_dump(mode="json") if current else None
        for candidate in (admitted, related[identity] or {}):
            evidence_ids.update(candidate.get("supporting_evidence_ids", []))
            evidence_ids.update(candidate.get("refuting_evidence_ids", []))
    # Current related changes are DATA; the original admitted snapshot remains
    # in the session binding and is never silently replaced with this value.
    changes["current_related_candidates"] = related
    if evidence_ids:
        records = await deps.evidence_store.list_for_run(runtime.run_id)
        changes["related_evidence_validity"] = [(await current_validity(
            deps.evidence_store, graph, str(runtime.run_id), identity, records=records,
        )).model_dump(mode="json") for identity in sorted(evidence_ids)]
    intents = await deps.central_state.list_for_mission(runtime.mission_id)
    for record in intents:
        if record.status.value == "applied" and record.run_id in (None, runtime.run_id):
            changes["operator:" + str(record.intent_id)] = record.model_dump(mode="json")
    usage = await deps.central_state.transaction(
        lambda tx: tx.probe_usage(str(runtime.active_task_id)), write=False,
    ) if task.get("probe_limits") else None
    if usage:
        limits = task["probe_limits"]
        warnings = {key: limits[limit] - usage[key] for key, limit in (
            ("actions", "max_actions"), ("model_requests", "max_model_requests"),
        ) if limits[limit] - usage[key] <= 1}
        if warnings:
            changes["budget_boundary"] = warnings
    previous = dict(session.notifications)
    changed = {key: value for key, value in changes.items()
               if previous.get(key) != content_hash(value)}
    previous.update({key: content_hash(value) for key, value in changes.items()})
    # The initial user display already contains current Task/scope/operator DATA.
    # On later turns, publish only actual changes, deduplicated by identity/hash.
    if session.notifications and changed:
        session = session.append(FrozenMessage.capture({"role": "user", "content": json.dumps({
            "worker_updates": changed, "note": "Current DATA; earlier evidence windows remain "
                                                 "historical observations, not fresh permission.",
        }, ensure_ascii=False)}, max_content_chars=NATIVE_INPUT_LIMIT.max_bytes))
    return session.model_copy(update={"notifications": tuple(sorted(previous.items()))})
