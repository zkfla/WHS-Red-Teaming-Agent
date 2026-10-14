"""Native execution's central state boundary, without Path or a Work scheduler.

The existing engine/nodes/InstanceDriver retain judgment and execution. These
functions bind their original identities, ownership and prepared writes to SQL.
Explicit selection and recovery reuse this boundary; goal close is plan2d.
"""

from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import datetime, timezone

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.central import CentralAttackGraphStore
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.convert import execution_input_digest
from redteam.core.enums import (
    AgentPhase,
    MissionStatus,
    OperatorIntentType,
    PolicyVerdict,
    TaskStatus,
)
from redteam.core.ids import AgentInstanceId, EventId, new_id
from redteam.core.tool_binding import execution_fingerprint
from redteam.orchestration.central_graph_batch import CentralGraphBatch
from redteam.orchestration.central_state import check_evidence
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.execution_journal import append_execution_receipt, preserved_result
from redteam.orchestration.operator_intents.models import IntentStatus
from redteam.orchestration.operator_intents.service import is_task_suppressed
from redteam.orchestration.resource_conflicts import execution_resource_keys
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.work.models import work_owner_for


def event(state, kind, payload, *, event_id=None):
    return EventEnvelope(
        event_id=EventId(event_id or new_id("evt")),
        event_type=kind,
        run_id=state.run_id,
        engagement_id=state.engagement_id,
        timestamp=datetime.now(timezone.utc),
        source="orchestration.central",
        correlation_id=state.run_id,
        payload=payload,
    )


class CentralEventBus:
    """Queue before notification, using the existing central outbox."""

    def __init__(self, state, bus):
        self.state, self.bus = state, bus

    async def publish(self, envelope):
        await self.state.transaction(lambda tx: tx.queue(envelope))
        await self.state.deliver_outbox(self.bus)

    async def subscribe(self, kind, handler):
        await self.bus.subscribe(kind, handler)

    async def flush(self):
        return await self.state.deliver_outbox(self.bus)


async def process_controls(deps):
    state = deps.central_state
    assert state is not None
    mission_id = await state.transaction(lambda tx: tx.binding()[0], write=False)
    pending = await state.list_for_mission(mission_id, status=IntentStatus.RECEIVED)
    for original in pending:
        now = datetime.now(timezone.utc)
        notification = event(
            state, EventType.OPERATOR_INTENT_APPLIED, {"intent_id": str(original.intent_id)}
        )

        def apply(tx, original=original, now=now, notification=notification):
            saved = tx.get_intent(str(original.intent_id))
            if saved is None or saved.status is not IntentStatus.RECEIVED:
                return
            mission = tx.get_mission(tx.binding()[0])
            kind = saved.intent.intent_type
            preferences = dict(mission.operator_preferences)
            status = mission.status
            supported = kind in {
                OperatorIntentType.PAUSE,
                OperatorIntentType.STOP,
                OperatorIntentType.EXCLUDE,
                OperatorIntentType.RESUME,
                OperatorIntentType.STATUS_QUERY,
                OperatorIntentType.EXPLANATION_QUERY,
                OperatorIntentType.GUIDANCE,
                OperatorIntentType.FOCUS,
                OperatorIntentType.REPRIORITIZE,
            }

            query = kind in {OperatorIntentType.STATUS_QUERY, OperatorIntentType.EXPLANATION_QUERY}
            if mission.status in {
                MissionStatus.CANCELLED,
                MissionStatus.COMPLETED,
                MissionStatus.FAILED,
            }:
                supported = query
            rejection_reason = "central_development_intent_not_supported"
            if kind is OperatorIntentType.PAUSE:
                status = MissionStatus.PAUSED
            elif kind is OperatorIntentType.STOP:
                status = MissionStatus.CANCELLED
            elif kind is OperatorIntentType.RESUME:
                status = MissionStatus.ACTIVE
            elif kind is OperatorIntentType.EXCLUDE:
                params = saved.intent.parameters
                for source, key in (
                    ("exclude_areas", "excluded_areas"),
                    ("endpoint_selectors", "excluded_endpoints"),
                    ("task_selectors", "excluded_task_ids"),
                ):
                    incoming = params.get(source, [])
                    if not isinstance(incoming, list) or any(
                        not isinstance(i, str) for i in incoming
                    ):
                        raise StateConflict("operator_exclusion_shape_invalid")
                    preferences[key] = list(dict.fromkeys([*preferences.get(key, []), *incoming]))
                preferences["excluded_task_ids"] = list(
                    dict.fromkeys(
                        [
                            *preferences.get("excluded_task_ids", []),
                            *map(str, saved.intent.task_ids),
                        ]
                    )
                )
            elif kind is OperatorIntentType.GUIDANCE:
                preferences["guidance"] = saved.intent.instruction
            elif kind is OperatorIntentType.FOCUS:
                preferences["focus_areas"] = saved.intent.parameters.get("focus_areas", [])
            if kind is OperatorIntentType.REPRIORITIZE and supported:
                params = saved.intent.parameters
                if any(key in params for key in ("scope", "approval", "budget", "target")):
                    supported = False
                    rejection_reason = "operator_reprioritize_forbidden_field"
                else:
                    areas = params.get("focus_areas", [])
                    endpoints = params.get("endpoint_selectors", [])
                    selectors = params.get("task_selectors", [])
                    def _str_list(value):
                        return isinstance(value, list) and all(
                            isinstance(i, str) for i in value
                        )
                    shapes_ok = (
                        _str_list(areas)
                        and _str_list(endpoints)
                        and _str_list(selectors)
                    )
                    existing = {str(task.task_id) for task in tx.list_tasks()}
                    named = [str(t) for t in saved.intent.task_ids]
                    unknown = [t for t in named if t not in existing]
                    def _sel_hit(sel):
                        token = sel.strip()
                        return bool(token) and any(
                            token in tid or tid in token for tid in existing
                        )
                    matched_selector = not selectors or any(
                        _sel_hit(sel) for sel in selectors
                    )
                    empty = not named and not selectors and not areas and not endpoints
                    if not shapes_ok or unknown or not matched_selector or empty:
                        supported = False
                        if unknown or not matched_selector:
                            rejection_reason = "operator_reprioritize_unknown_task"
                        else:
                            rejection_reason = "operator_reprioritize_empty_or_malformed"
                    else:
                        ordered = list(dict.fromkeys([
                            *named,
                            *[s.strip() for s in selectors if s.strip()],
                        ]))
                        stored_ids = preferences.get("prioritized_task_ids", []) or []
                        kept = [i for i in stored_ids if isinstance(i, str)][-100:]
                        pending = [t for t in ordered if t not in kept]
                        preferences["prioritized_task_ids"] = (kept + pending)[-100:]
                        if areas:
                            preferences["focus_areas"] = areas
                        if endpoints:
                            stored = preferences.get("focus_endpoints", []) or []
                            current = [i for i in stored if isinstance(i, str)]
                            merged = list(dict.fromkeys([*current, *endpoints]))
                            preferences["focus_endpoints"] = merged[-100:]
            result = saved.model_copy(
                update={
                    "status": IntentStatus.APPLIED if supported else IntentStatus.REJECTED,
                    "applied_at": now if supported else None,
                    "applied_event_id": notification.event_id if supported else None,
                    "outcome": f"{kind.value}_applied" if supported else "unsupported_or_closed",
                    "reason": None if supported else rejection_reason,
                }
            )

            def mutate(target):
                if query:
                    return
                target.update_mission(
                    mission.model_copy(
                        update={
                            "status": status,
                            "operator_preferences": preferences,
                            "revision": mission.revision + 1,
                            "updated_at": now,
                        }
                    ),
                    expected_revision=mission.revision,
                )

            tx.apply_intent(result, mutate)
            tx.queue(
                notification.model_copy(
                    update={
                        "event_type": (
                            EventType.OPERATOR_INTENT_APPLIED
                            if supported
                            else EventType.OPERATOR_INTENT_REJECTED
                        ),
                    }
                )
            )

        await state.transaction(apply)
    flush = getattr(deps.event_bus, "flush", None)
    if flush is not None:
        await flush()


async def runtime_token(deps, runtime, *, require_task=True, admit=True):
    state = deps.central_state
    from redteam.orchestration.specialist_planner import specialist_planning_inputs

    assert state is not None
    if (
        str(runtime.run_id) != state.run_id
        or str(runtime.engagement_id) != state.engagement_id
        or runtime.component_binding != deps.component_binding
        or runtime.llm_request_policy != deps.settings.llm_request_policy.model_dump(mode="json")
        or runtime.active_path_id is not None
    ):
        raise StateConflict("central_runtime_original_binding_mismatch")
    expected = content_hash(specialist_planning_inputs(deps))

    def read(tx):
        mission_id, binding = tx.binding()
        if mission_id != str(runtime.mission_id) or binding != expected:
            raise StateConflict("central_runtime_original_binding_mismatch")
        mission = tx.admit(settlement=not require_task) if admit else tx.get_mission(mission_id)
        if runtime.started_at != mission.created_at:
            raise StateConflict("central_original_run_clock_changed")
        task = tx.get_task(str(runtime.active_task_id)) if runtime.active_task_id else None
        if require_task and (
            task is None
            or task.occupant_owner != runtime.work_owner
            or task.ownership_version != runtime.task_ownership_version
            or task.assigned_agent != runtime.current_agent
        ):
            raise StateConflict("stale_task_ownership")
        from redteam.core.contracts.completion import completion_contract_hash

        return (
            mission.revision,
            content_hash(mission),
            completion_contract_hash(task) if task else None,
            task.ownership_version if task else None,
        )

    return await state.transaction(read, write=False)


async def ready(deps, base):
    workers = await ready_workers(deps, base, max_instances=1)
    return workers[0] if workers else None


def worker_limit(deps):
    return min(deps.settings.global_concurrency, deps.specialist_policy.max_instances)


def tool_limit(deps):
    limits = [
        deps.settings.global_concurrency,
        deps.engagement.rate_limit_policy.max_concurrent_actions,
    ]
    limits.extend(
        rule.max_parallel_actions
        for rule in deps.engagement.rules_of_engagement
        if rule.max_parallel_actions is not None
    )
    return min(limits)


def _candidate_order(tasks, mission):
    prefs = mission.operator_preferences or {}
    prioritized = [str(i) for i in (prefs.get("prioritized_task_ids", []) or []) if str(i).strip()]
    rank = {tid: index for index, tid in enumerate(prioritized)}
    focus = [str(a).lower() for a in (prefs.get("focus_areas", []) or []) if str(a).strip()]
    endpoints = [str(e) for e in (prefs.get("focus_endpoints", []) or []) if str(e).strip()]

    def key(task):
        tid = str(task.task_id)
        ordered = rank.get(tid)
        if ordered is not None:
            return (0, ordered, 0, tid)
        haystack = (task.title + " " + task.objective).lower()
        nodes = {str(n) for n in task.related_attack_graph_node_ids}
        focused = any(area in haystack for area in focus) or any(
            sel and (sel in nodes or any(sel in node for node in nodes)) for sel in endpoints
        )
        return (1, 0, 0 if focused else 1, tid)

    return sorted(tasks, key=key)


def task_is_claimable(
    task,
    *,
    eligible_roles: Collection[str] | None,
    mission,
    require_probe_limits: bool,
    candidate_revisions: Mapping[str, int] | None = None,
    allow_running: bool = False,
) -> bool:
    """Pure claimability check shared by worker admission and planner witnesses.

    The ordinary path mirrors ``ready_workers``: only pending/ready, unowned,
    unsuppressed tasks assigned to an eligible role can be claimed; required
    probe limits and every captured candidate revision must also be current.
    A planner may opt to recognize an already-running, owned task as a witness.
    That task keeps its captured purpose and is not rebound to projected candidate
    revisions. This mode never admits blocked or approval-waiting work.
    """
    if eligible_roles is None or mission is None:
        return False
    try:
        roles = {str(role) for role in eligible_roles}
    except TypeError:
        return False
    if not roles:
        return False
    status = getattr(task, "status", None)
    owner = getattr(task, "occupant_owner", None)
    claimable = status in {TaskStatus.PENDING, TaskStatus.READY} and owner is None
    running = (
        allow_running and status == TaskStatus.RUNNING
        and isinstance(owner, str) and bool(owner.strip())
        and getattr(task, "ownership_version", 0) > 0
    )
    if not (claimable or running):
        return False
    assigned = getattr(task, "assigned_agent", None)
    if assigned is None or str(assigned) not in roles:
        return False
    if is_task_suppressed(task, mission):
        return False
    if require_probe_limits and getattr(task, "probe_limits", None) is None:
        return False
    if running:
        return True
    revisions = candidate_revisions or {}
    return all(
        identity in revisions and revisions[identity] == revision
        for identity, revision in getattr(task, "hypothesis_revisions", {}).items()
    )


async def ready_workers(deps, base, *, max_instances=None):

    from redteam.orchestration.agents.tool_access import ready_role_ids

    state = deps.central_state
    await process_controls(deps)
    eligible = await ready_role_ids(deps)
    limit = min(worker_limit(deps), max_instances or worker_limit(deps))
    candidates = await state.list_tasks()

    def claim(tx):
        mission = tx.admit()
        slots = limit - tx.active_activation_count()
        claimed = []
        for candidate in _candidate_order(candidates, mission):
            if slots <= 0:
                break
            task = tx.get_task(str(candidate.task_id))
            snapshots = {}
            candidate_revisions = {}
            if task.hypothesis_revisions:
                from redteam.attack_graph.identity import hypothesis_node_id
                from redteam.attack_graph.predicates import facts_of
                from redteam.core.contracts.graph import Hypothesis

                for identity in task.hypothesis_revisions:
                    node = tx.graph.get_local_node(hypothesis_node_id(identity))
                    candidate = (Hypothesis.model_validate(facts_of(node.properties))
                                 if node else None)
                    if candidate is None:
                        break
                    candidate_revisions[identity] = candidate.revision
                    snapshots[identity] = candidate.model_dump(mode="json")
            if not task_is_claimable(
                task,
                eligible_roles=eligible,
                mission=mission,
                require_probe_limits=deps.specialist_policy.probe_limits is not None,
                candidate_revisions=candidate_revisions,
            ):
                continue  # The planner refreshes changed purposes; admission stays fail-closed.
            if not task.hypothesis_revisions or task.probe_limits is None:
                snapshots = None
            instance = AgentInstanceId(new_id("inst"))
            owner = work_owner_for(base.run_id, instance)
            claimed.append(
                (instance, tx.claim_task(str(task.task_id), owner=owner, instance_id=str(instance)),
                 snapshots)
            )
            slots -= 1
        return claimed

    try:
        claimed = await state.transaction(claim)
    except CoordinationError:
        return []
    return [
        base.model_copy(
            deep=True,
            update={
                "instance_id": instance,
                "active_task_id": task.task_id,
                "task_ownership_version": task.ownership_version,
                "current_agent": task.assigned_agent,
                "phase": AgentPhase.OBSERVE,
                "termination_reason": None,
                "pending_action_id": None,
                "pending_approval_id": None,
                "probe_hypotheses": snapshots,
            },
        )
        for instance, task, snapshots in claimed
    ]


ACTOR_RETURNED = "InvestigationActorReturned"


async def actor_returned(deps, runtime, *, failed=False):
    state = deps.central_state
    notification = event(
        state,
        ACTOR_RETURNED,
        {
            "task_id": str(runtime.active_task_id),
            "instance_id": str(runtime.instance_id),
        },
    )

    def commit(tx):
        tx.actor_returned(
            str(runtime.active_task_id),
            owner=runtime.work_owner,
            version=runtime.task_ownership_version,
            instance_id=str(runtime.instance_id),
            failed=failed,
        )
        tx.queue(notification)

    await state.transaction(commit)
    await deps.event_bus.flush()


async def entering(deps, runtime):
    await runtime_token(deps, runtime)
    await deps.central_state.transaction(
        lambda tx: tx.entering(
            str(runtime.active_task_id),
            owner=runtime.work_owner,
            version=runtime.task_ownership_version,
            instance_id=str(runtime.instance_id),
        )
    )


async def observe(state, deps):
    runtime = state["runtime"]
    await process_controls(deps)
    mission = await deps.central_state.get_mission(runtime.mission_id)
    reason = (
        TerminationReason.OPERATOR_PAUSED
        if mission.status is MissionStatus.PAUSED
        else TerminationReason.OPERATOR_STOPPED
        if mission.status is MissionStatus.CANCELLED
        else None
    )
    if reason:
        return {**state, "runtime": runtime.model_copy(update={"termination_reason": reason})}
    await runtime_token(deps, runtime)
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    if task.status is TaskStatus.COMPLETED:
        runtime.termination_reason = TerminationReason.INVESTIGATION_HELD
        return state
    if task.status is TaskStatus.BLOCKED:
        runtime.termination_reason = (
            TerminationReason.APPROVAL_PENDING
            if str(task.blocking_reason).startswith("approval:")
            else TerminationReason.RESOURCE_WAIT
        )
        return state
    service = AttackGraphService(deps.graph_store)
    observation = await service.observe(
        step=runtime.step_count + 1,
        max_per_type=deps.settings.observation_max_per_type,
        max_total=deps.settings.observation_max_total,
    )
    return {
        **state,
        "observation": observation,
        "runtime": runtime.model_copy(
            update={
                "step_count": runtime.step_count + 1,
                "phase": AgentPhase.PLAN,
            }
        ),
    }


async def register_submission(deps, runtime, step, proposal, invocation, decision):
    from redteam.core.contracts.completion import completion_contract_hash

    state = deps.central_state
    task = await state.get_task(str(runtime.active_task_id))
    # Central receipts bind the complete original question contract, not a path.
    step = step.model_copy(update={"task_purpose_hash": completion_contract_hash(task)})
    receipt = await append_execution_receipt(
        deps.evidence_store,
        invocation=invocation,
        proposal=proposal,
        step=step,
        work_id=None,
        reference=(deps.execution_recovery.prepare_reference(invocation)
                   if deps.execution_recovery is not None else None),
        accounting_before={
            "actions_executed": runtime.actions_executed,
            "remaining_budget": runtime.remaining_budget,
        },
    )
    checked = await check_evidence(
        deps.evidence_store,
        run_id=state.run_id,
        engagement_id=state.engagement_id,
        evidence_ids=[str(receipt.evidence_id)],
    )
    mission = await state.get_mission(runtime.mission_id)
    tool = deps.tool_catalog.get(invocation.tool_id)
    record = TaskExecution(
        engagement_id=runtime.engagement_id,
        run_id=runtime.run_id,
        task_id=runtime.active_task_id,
        instance_id=runtime.instance_id,
        ownership_version=runtime.task_ownership_version,
        goal_revision=mission.revision,
        action_id=proposal.action_id,
        invocation_id=invocation.invocation_id,
        model_tool_call_id=step.model_tool_call_id,
        tool_id=invocation.tool_id,
        input_digest=execution_input_digest(invocation),
        proposal_evidence_id=receipt.evidence_id,
        target_node_id=str(node_id_for_target(proposal.target)) if proposal.target else None,
        target_identity_verified=True,
        approval_ref=str(decision.approval_id) if decision.approval_id else None,
        approval_fingerprint=(
            execution_fingerprint(invocation.tool_id, proposal.target, proposal.arguments)
            if decision.approval_id
            else None
        ),
        wait_reason="approval" if decision.verdict is PolicyVerdict.REQUIRE_APPROVAL else None,
        resource_keys=execution_resource_keys(tool, invocation, step.principal_ref),
        resource_lease_until=datetime.now(timezone.utc).timestamp()
        + deps.settings.lease_ttl_seconds,
    )
    try:
        await state.transaction(
            lambda tx: tx.record_execution(
                record,
                owner=runtime.work_owner,
                checked=checked,
                now=datetime.now(timezone.utc).timestamp(),
                max_calls=tool_limit(deps),
                per_target=deps.settings.per_target_concurrency,
            )
        )
    except StateConflict as exc:
        if str(exc) != "shared_resource_execution_conflict":
            raise
        record = record.model_copy(update={"wait_reason": "resource", "resource_lease_until": 0})
        await state.transaction(
            lambda tx: tx.record_execution(
                record,
                owner=runtime.work_owner,
                checked=checked,
                now=datetime.now(timezone.utc).timestamp(),
                max_calls=tool_limit(deps),
                per_target=deps.settings.per_target_concurrency,
            )
        )
    if decision.verdict is PolicyVerdict.DENY:
        record = (await state.get_execution(str(invocation.invocation_id))).model_copy(
            update={"state": ExecutionState.CANCELLED, "wait_reason": None}
        )
        await state.transaction(
            lambda tx: tx.update_execution(
                record,
                owner=runtime.work_owner,
                now=datetime.now(timezone.utc).timestamp(),
            )
        )
    return await state.get_execution(str(invocation.invocation_id))


async def start_execution(deps, runtime, invocation, step):
    revision, *_ = await runtime_token(deps, runtime)
    record = await deps.central_state.get_execution(str(invocation.invocation_id))
    if record is None or record.state is not ExecutionState.PENDING or record.wait_reason:
        raise StateConflict("central_execution_cannot_be_replayed")

    def start(tx):
        tx.check_execution_capacity(
            record, max_calls=tool_limit(deps), per_target=deps.settings.per_target_concurrency
        )
        return tx.update_execution(
            record.model_copy(
                update={
                    "state": ExecutionState.RUNNING,
                    "resource_execution_pending": bool(record.resource_keys),
                }
            ),
            owner=runtime.work_owner,
            ownership_version=runtime.task_ownership_version,
            admitted_revision=revision,
            comparison_keys=execution_resource_keys(
                deps.tool_catalog.get(invocation.tool_id), invocation, step.principal_ref,
            ),
            now=datetime.now(timezone.utc).timestamp(),
        )

    await deps.central_state.transaction(start)


async def abandon(deps, runtime):
    state = deps.central_state
    for record in await state.list_executions(task_id=str(runtime.active_task_id)):
        if record.state is ExecutionState.RUNNING:
            await state.transaction(
                lambda tx, record=record: tx.update_execution(
                    record.model_copy(update={"state": ExecutionState.UNKNOWN}),
                    owner=runtime.work_owner,
                    ownership_version=runtime.task_ownership_version,
                    now=datetime.now(timezone.utc).timestamp(),
                )
            )


async def run_call(call, state, deps):
    """Reuse Control -> execute -> verify -> update; one prepared SQL result commit."""
    from redteam.orchestration.nodes.execute import execute_node
    from redteam.orchestration.nodes.policy import policy_node

    runtime, item = state["runtime"], call.dispatch
    await process_controls(deps)
    local = {
        **state,
        "plan_step": item.plan_step,
        "action_proposal": item.proposal,
        "policy_decision": None,
        "tool_result": None,
        "current_work_id": None,
        "prepared_invocation": item.invocation,
        "current_execution_id": str(item.invocation.invocation_id),
    }
    previous = await deps.central_state.get_execution(local["current_execution_id"])
    if previous is None and await deps.central_state.transaction(
        lambda tx: tx.policy_budget_remaining(), write=False,
    ) == 0:
        state["runtime"].termination_reason = TerminationReason.BUDGET_EXHAUSTED
        return state
    if previous is not None:
        # A completed checkpoint may lag its central commit. Restore its refs
        # without Control/Gateway/usage replay; unknowns always remain held.
        if (
            previous.state
            in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
            and not previous.wait_reason
        ):
            call.evidence_ids = (
                [str(previous.result_evidence_id)] if previous.result_evidence_id else []
            )
            call.completed = True
            call.feedback = {
                "ok": previous.state is ExecutionState.SUCCEEDED,
                "executed": previous.state is not ExecutionState.CANCELLED,
                "restored": True,
            }
        elif previous.state is ExecutionState.PENDING:
            from redteam.orchestration.central_recovery import resume_intent

            local = await resume_intent(deps, local, previous)
            if local is None:
                current = await deps.central_state.get_execution(str(previous.invocation_id))
                if current is None:
                    raise StateConflict("original_execution_missing")
                state["runtime"].termination_reason = (
                    TerminationReason.APPROVAL_PENDING if current.wait_reason == "approval"
                    else TerminationReason.RESOURCE_WAIT
                )
                return state
            decision = local["policy_decision"]
            if decision.verdict is not PolicyVerdict.ALLOW:
                call.completed = True
                call.feedback = {"ok": False, "executed": False, "error": decision.reasons}
                state["runtime"] = local["runtime"].model_copy(update={
                    "pending_action_id": None, "pending_approval_id": None,
                    "termination_reason": None,
                })
                return state
            local = await execute_node(local, deps=deps)
            return await _finish_call(deps, state, call, local, item)
        else:
            state["runtime"].termination_reason = (
                TerminationReason.APPROVAL_PENDING
                if previous.wait_reason == "approval"
                else TerminationReason.RESOURCE_WAIT
            )
        return state
    # Original Control charging and durable capacity reservation are serialized
    # across workers. Provider execution stays outside this lock.
    async with deps.policy_lock:
        local = await policy_node(local, deps=deps)
        decision = local["policy_decision"]
        record = await register_submission(
            deps, runtime, item.plan_step, item.proposal, item.invocation, decision
        )
    if record.wait_reason == "resource":
        state["runtime"].termination_reason = TerminationReason.RESOURCE_WAIT
        return state
    if decision.verdict is not PolicyVerdict.ALLOW:
        call.feedback = {
            "ok": False,
            "executed": False,
            "error": decision.reasons,
            "approval_id": record.approval_ref,
        }
        call.completed = decision.verdict is PolicyVerdict.DENY
        state["runtime"] = local["runtime"].model_copy(
            update={
                "pending_action_id": item.proposal.action_id if record.approval_ref else None,
            }
        )
        return state
    local = await execute_node(local, deps=deps)
    return await _finish_call(deps, state, call, local, item)


async def _finish_call(deps, state, call, local, item):
    local, result, unknown = await commit_result(deps, local, item)
    call.evidence_ids = [str(local["result_evidence_id"])]
    call.feedback = {
        "ok": result.success and not unknown,
        "executed": True,
        "evidence_ids": call.evidence_ids,
        "unknown": unknown,
    }
    call.completed = not unknown
    state["runtime"] = local["runtime"]
    if unknown:
        state["runtime"].termination_reason = TerminationReason.RESOURCE_WAIT
    return state


async def commit_result(deps, local, item):
    """One consumer for live and recovered originals; no provider execution."""
    from redteam.core.contracts.tool import execution_outcome_uncertain
    from redteam.orchestration.nodes.update import update_node
    from redteam.orchestration.nodes.verify import verify_node

    runtime = local["runtime"]
    batch = CentralGraphBatch(CentralAttackGraphStore(deps.central_state), deps.central_state)
    processing = replace(
        deps,
        graph_store=EngagementScopedAttackGraphStore(
            batch,
            engagement_id=runtime.engagement_id,
        ),
        event_bus=batch,
    )
    try:
        local = await verify_node(local, deps=processing)
        local = await update_node(local, deps=processing)
        original = await preserved_result(
            deps.evidence_store,
            run_id=runtime.run_id,
            action_id=item.proposal.action_id,
            instance_id=runtime.instance_id,
            work_id=None,
        )
        if original is None:
            raise StateConflict("central_original_result_missing")
        result, receipt_id = original
        from redteam.orchestration.resource_cleanup import apply_resource_effects

        await apply_resource_effects(
            AttackGraphService(processing.graph_store), result=result, runtime=runtime,
            evidence_id=receipt_id,
        )
        unknown = execution_outcome_uncertain(result.metadata)
        current = await deps.central_state.get_execution(str(result.invocation_id))
        job = current.job_ref
        update = current.model_copy(
            update={
                "state": (
                    ExecutionState.UNKNOWN
                    if unknown
                    else ExecutionState.SUCCEEDED
                    if result.success
                    else ExecutionState.FAILED
                ),
                "result_evidence_id": receipt_id,
                "wait_reason": "job" if job and unknown else None,
                "job_ref": job if unknown else None,
            }
        )
        identities = [str(receipt_id)]
        if local.get("result_evidence_id"):
            identities.append(str(local["result_evidence_id"]))
        checked = await check_evidence(
            deps.evidence_store,
            run_id=deps.central_state.run_id,
            engagement_id=deps.central_state.engagement_id,
            evidence_ids=identities,
        )
        # A verified resolution is a new immutable original for the SAME
        # invocation. Its graph interpretation cannot reuse the uncertain
        # envelope's mutation identity with different contents.
        nodes, edges = await batch.prepare(f"{result.invocation_id}:{receipt_id}")

        def commit(tx):
            tx.update_execution(
                update,
                owner=runtime.work_owner,
                ownership_version=runtime.task_ownership_version,
                checked=checked,
                now=datetime.now(timezone.utc).timestamp(),
            )
            batch.commit(tx, nodes, edges)

        await deps.central_state.transaction(commit)
        if not unknown:
            from redteam.orchestration.execution_journal import mark_execution_processed

            await mark_execution_processed(
                deps.evidence_store,
                run_id=runtime.run_id,
                action_id=item.proposal.action_id,
                instance_id=str(runtime.instance_id),
                work_id=None,
                evidence_id=local["result_evidence_id"],
            )
        await deps.event_bus.flush()
        return local, result, unknown
    except BaseException:
        await abandon(deps, runtime)
        raise
