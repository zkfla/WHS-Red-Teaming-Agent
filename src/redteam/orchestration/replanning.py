"""Dynamic replanning: turn new knowledge in the Attack Graph into Task Graph
changes, and evaluate Mission progress.

This is the piece that makes the Orchestrator more than a static checklist: as
recon/web work reveals attack surface, the Task Graph grows to match. The
canonical case is an object-bound API endpoint (``/api/orders/{id}``): its
existence implies an authorization question, so a dedicated assessment Task is
created. Task creation coincides with a graph change, so the existing
"graph changed -> continue" replan path keeps the run going and OBSERVE
schedules the new Task on the next turn (Task-first).

It never touches the Attack Graph, Work Ledger or Evidence — only the Task
Graph — and never stores a secret.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.identity import host_node_id
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import MissionCriterionResult, MissionTask, Provenance
from redteam.core.enums import EvidenceSourceType, GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import EventId, EvidenceId, GraphNodeId, MissionId, RunId, TaskId, new_id
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.operator_intents.suppression import endpoint_excluded

#: Provenance marker for endpoint-scoped second-principal acquisition Tasks.
_ACQUIRE_SOURCE = "replan:acquire_second_principal"
_DEPENDENCY_BLOCK_REASON = "required dependency is blocked"


def _conclusive_session_comparison(item: object) -> bool:
    """Whether a session-comparison observation actually answers the
    object-bound authorization question.

    At least one identity must reach the object. The other must reach it or
    receive an explicit permission denial. Unauthenticated, redirected,
    missing-object and server-error responses leave the assessment unresolved.
    This proves a comparison was performed, not that authorization is secure.
    """

    if not isinstance(item, dict) or item.get("kind") != "http_session_comparison":
        return False
    first, second = item.get("first_status"), item.get("second_status")
    if type(first) is not int or type(second) is not int:
        return False
    return (200 <= first < 300 and (200 <= second < 300 or second == 403)) or (
        200 <= second < 300 and first == 403
    )


async def replan_task_graph(
    store: MissionTaskStore,
    graph: AttackGraphService,
    mission_id: MissionId,
    *,
    publisher: EventPublisher,
    last_event_id: EventId | None,
    evidence_store: EvidenceStore | None = None,
    run_id: RunId | None = None,
    now: datetime | None = None,
) -> EventId | None:
    """Create a Task for each newly-discovered object-bound endpoint.

    Idempotent: an endpoint already covered by a Task (by
    ``related_attack_graph_node_ids``) is skipped, so replanning every turn
    never duplicates work.
    """

    now = now or datetime.now(timezone.utc)
    mission = await store.get_mission(mission_id)
    excluded_endpoints = (
        list(mission.operator_preferences.get("excluded_endpoints") or [])
        if mission is not None
        else []
    )
    excluded_areas = (
        set(mission.operator_preferences.get("excluded_areas") or [])
        if mission is not None
        else set()
    )
    nodes = await graph.list_nodes()
    principals_by_target: dict[str, set[str]] = {}
    for principal in nodes:
        if principal.type is not GraphNodeType.PRINCIPAL:
            continue
        facts = facts_of(principal.properties)
        target_ref = facts.get("target_ref")
        if isinstance(target_ref, str):
            principal_ref = facts.get("principal_ref")
            if "/session:" in str(principal.id):
                # Session identities need a usable comparison from this run;
                # cached mappings alone cannot establish available credentials.
                continue
            principals_by_target.setdefault(target_ref, set()).add(
                principal_ref if isinstance(principal_ref, str) else str(principal.id)
            )

    existing = await store.list_tasks(mission_id=mission_id)
    covered = {
        str(node_id)
        for task in existing
        if task.provenance.source == "replan:object_bound_endpoint"
        for node_id in task.related_attack_graph_node_ids
    }
    acquisition_by_endpoint = {
        str(node_id): task.task_id
        for task in existing
        if task.provenance.source == _ACQUIRE_SOURCE
        for node_id in task.related_attack_graph_node_ids
    }

    for node in nodes:
        if node.type is not GraphNodeType.ENDPOINT:
            continue
        if excluded_areas.intersection({"web", "api", "authorization"}) or endpoint_excluded(
            node.id, excluded_endpoints
        ):
            continue
        params = facts_of(node.properties).get("path_parameters")
        if not params or str(node.id) in covered:
            continue
        endpoint_facts = facts_of(node.properties)
        host = endpoint_facts.get("host")
        host_node = await graph.get_node(host_node_id(host)) if isinstance(host, str) else None
        host_facts = facts_of(host_node.properties) if host_node else {}
        target_ref = str(
            endpoint_facts.get("target_ref") or host_facts.get("target_ref") or host or ""
        )
        sessions = host_facts.get("auth_session_config", {}).get("sessions", [])
        path = endpoint_facts.get("path")
        usable_refs = {
            item.get("session_ref")
            for item in sessions
            if isinstance(item, dict)
            and isinstance(path, str)
            and path in item.get("paths", [])
            and isinstance(item.get("session_ref"), str)
        }
        # An object-level authorization check compares two principals' access
        # to the same object. With fewer than two principals known, the check
        # is blocked on acquiring a second one, expressed as a dependency Task.
        needs_second_principal = len(principals_by_target.get(target_ref, set())) < 2
        auth_task = MissionTask(
            task_id=TaskId(new_id("task")),
            mission_id=mission_id,
            title=f"Authorization assessment: {node.id}",
            objective=(f"Verify object-level authorization on the object-bound endpoint {node.id}"),
            status=TaskStatus.READY,
            #: High value: object-bound authz outranks the generic role sweeps.
            priority=100,
            required_capability="web",
            related_attack_graph_node_ids=[node.id],
            provenance=Provenance(source="replan:object_bound_endpoint"),
            created_at=now,
            updated_at=now,
        )
        await store.create_task(auth_task)
        last_event_id = await _publish_task_created(
            publisher, auth_task.task_id, "object_bound_endpoint", str(node.id), last_event_id
        )

        if needs_second_principal:
            acquire_task_id = acquisition_by_endpoint.get(str(node.id))
            if acquire_task_id is None:
                acquire = MissionTask(
                    task_id=TaskId(new_id("task")),
                    mission_id=mission_id,
                    title="Acquire a second principal",
                    objective="Obtain a second principal/session to compare authorization",
                    status=TaskStatus.READY,
                    priority=101,
                    required_capability="web",
                    related_attack_graph_node_ids=[node.id],
                    provenance=Provenance(
                        source=_ACQUIRE_SOURCE,
                        metadata={"target_ref": target_ref, "endpoint_id": str(node.id)},
                    ),
                    created_at=now,
                    updated_at=now,
                )
                await store.create_task(acquire)
                acquire_task_id = acquire.task_id
                acquisition_by_endpoint[str(node.id)] = acquire_task_id
                last_event_id = await _publish_task_created(
                    publisher, acquire.task_id, "needs_second_principal", "", last_event_id
                )
                if len(usable_refs) < 2:
                    await store.block_task(
                        acquire_task_id,
                        "two distinct configured session references for this "
                        "target and path are unavailable",
                        now=now,
                    )
                    event = await publisher.publish(
                        EventType.TASK_BLOCKED,
                        {"task_id": acquire_task_id, "reason": "missing_session_references"},
                        causation_id=last_event_id,
                    )
                    last_event_id = event.event_id
            # Depend the authz check on the acquisition; add_dependency demotes
            # it READY -> PENDING so it is not scheduled until the dep completes.
            await store.add_dependency(auth_task.task_id, acquire_task_id, now=now)

    # A second principal is an observed fact, not proof that the Agent's
    # candidate frontier ran dry. Complete acquisition only when it exists.
    for endpoint_id, acquire_task_id in acquisition_by_endpoint.items():
        acquisition = await store.get_task(acquire_task_id)
        if acquisition is None:
            continue
        target_ref = acquisition.provenance.metadata.get("target_ref")
        if not isinstance(target_ref, str):
            endpoint = await graph.get_node(GraphNodeId(endpoint_id))
            target_ref = facts_of(endpoint.properties).get("target_ref") if endpoint else None
        if not isinstance(target_ref, str):
            continue
        supporting_ids = await _comparison_evidence_ids(
            acquisition,
            graph,
            evidence_store,
            run_id,
            mission.engagement_id if mission else None,
        )
        if len(principals_by_target.get(target_ref, set())) < 2 and not supporting_ids:
            continue
        if acquisition.status in {
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
        }:
            if acquisition.status is TaskStatus.BLOCKED:
                acquisition = await store.unblock_task(acquisition.task_id, now=now)
            if acquisition.status is TaskStatus.READY:
                await store.transition_task(acquisition.task_id, TaskStatus.RUNNING, now=now)
            await store.transition_task(acquisition.task_id, TaskStatus.COMPLETED, now=now)
            event = await publisher.publish(
                EventType.TASK_COMPLETED,
                {
                    "task_id": acquisition.task_id,
                    "reason": "second_principal_observed",
                    "evidence_ids": supporting_ids,
                },
                causation_id=last_event_id,
            )
            last_event_id = event.event_id

    # A measured two-session comparison answers the object-bound authorization
    # question even when it was performed while acquiring the second principal.
    for task in await store.list_tasks(mission_id=mission_id):
        if task.provenance.source != "replan:object_bound_endpoint" or task.status not in {
            TaskStatus.READY,
            TaskStatus.RUNNING,
        }:
            continue
        supporting_ids = await _comparison_evidence_ids(
            task,
            graph,
            evidence_store,
            run_id,
            mission.engagement_id if mission else None,
        )
        if supporting_ids:
            if task.status is TaskStatus.READY:
                await store.transition_task(task.task_id, TaskStatus.RUNNING, now=now)
            await store.transition_task(task.task_id, TaskStatus.COMPLETED, now=now)
            event = await publisher.publish(
                EventType.TASK_COMPLETED,
                {
                    "task_id": task.task_id,
                    "reason": "session_comparison_observed",
                    "evidence_ids": supporting_ids,
                },
                causation_id=last_event_id,
            )
            last_event_id = event.event_id

    # Dependency state is reflected in the authorization Task itself. A
    # blocked prerequisite must never leave required work looking runnable.
    for task in await store.list_tasks(mission_id=mission_id):
        if task.status not in {TaskStatus.PENDING, TaskStatus.BLOCKED}:
            continue
        if not task.dependency_task_ids:
            continue
        deps = [await store.get_task(dep) for dep in task.dependency_task_ids]
        if all(dep is not None and dep.status is TaskStatus.COMPLETED for dep in deps):
            if task.status is TaskStatus.PENDING:
                await store.transition_task(task.task_id, TaskStatus.READY, now=now)
            elif task.blocking_reason == _DEPENDENCY_BLOCK_REASON:
                await store.unblock_task(task.task_id, now=now)
                event = await publisher.publish(
                    EventType.TASK_UNBLOCKED,
                    {"task_id": task.task_id, "reason": "dependencies_completed"},
                    causation_id=last_event_id,
                )
                last_event_id = event.event_id
        elif task.status is TaskStatus.PENDING and any(
            dep is None
            or dep.status in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            for dep in deps
        ):
            await store.block_task(task.task_id, _DEPENDENCY_BLOCK_REASON, now=now)
            event = await publisher.publish(
                EventType.TASK_BLOCKED,
                {"task_id": task.task_id, "reason": "dependency_blocked"},
                causation_id=last_event_id,
            )
            last_event_id = event.event_id

    return last_event_id


async def _comparison_evidence_ids(
    task: MissionTask,
    graph: AttackGraphService,
    store: EvidenceStore | None,
    run_id: RunId | None,
    engagement_id: str | None,
) -> list[str]:
    if store is None or run_id is None or engagement_id is None:
        return []
    supporting: set[str] = set()
    for node_id in task.related_attack_graph_node_ids:
        endpoint = await graph.get_node(node_id)
        observations = facts_of(endpoint.properties).get("observations", []) if endpoint else []
        for item in observations:
            if not _conclusive_session_comparison(item):
                continue
            evidence_id = item.get("evidence_id")
            record = await store.get(EvidenceId(evidence_id)) if evidence_id else None
            if (
                record is not None
                and record.run_id == run_id
                and record.engagement_id == engagement_id
                and _authorization_evidence_matches(task, record)
            ):
                supporting.add(str(record.evidence_id))
    return sorted(supporting)


async def _publish_task_created(
    publisher: EventPublisher,
    task_id: TaskId,
    reason: str,
    endpoint: str,
    last_event_id: EventId | None,
) -> EventId | None:
    event = await publisher.publish(
        EventType.TASK_CREATED,
        {"task_id": task_id, "reason": reason, "endpoint": endpoint},
        causation_id=last_event_id,
    )
    return event.event_id


async def finalize_mission(
    store: MissionTaskStore,
    mission_id: MissionId,
    *,
    publisher: EventPublisher,
    last_event_id: EventId | None,
    evidence_store: EvidenceStore | None = None,
    run_id: RunId | None = None,
    now: datetime | None = None,
) -> EventId | None:
    """Settle the Mission without rewriting unfinished work as success."""

    mission = await store.get_mission(mission_id)
    if mission is None or mission.status in {
        MissionStatus.COMPLETED,
        MissionStatus.FAILED,
        MissionStatus.BLOCKED,
        # A paused Mission is suspended, not finished: settling it here would
        # rewrite PAUSED to BLOCKED and destroy the operator's intent to
        # resume the same Task. CANCELLED is already a deliberate outcome.
        MissionStatus.PAUSED,
        MissionStatus.CANCELLED,
    }:
        return last_event_id

    tasks = await store.list_tasks(mission_id=mission_id)
    now = now or datetime.now(timezone.utc)

    for task in tasks:
        if not task.required and task.status in {TaskStatus.PENDING, TaskStatus.READY}:
            await store.transition_task(task.task_id, TaskStatus.CANCELLED, now=now)
    tasks = await store.list_tasks(mission_id=mission_id)
    required = [task for task in tasks if task.required]
    evidence = (
        [
            item
            for item in await evidence_store.list_for_run(run_id)
            if item.engagement_id == mission.engagement_id
        ]
        if evidence_store is not None and run_id is not None
        else []
    )
    criterion_results = [
        _evaluate_criterion(criterion, tasks, evidence) for criterion in mission.success_criteria
    ]
    if any(task.status is TaskStatus.FAILED for task in required):
        outcome = MissionStatus.FAILED
    elif (
        tasks
        and all(task.status is TaskStatus.COMPLETED for task in required)
        and all(result.met for result in criterion_results)
    ):
        outcome = MissionStatus.COMPLETED
    else:
        outcome = MissionStatus.BLOCKED
    await store.update_mission(
        mission.model_copy(
            update={
                "status": outcome,
                "criterion_results": criterion_results,
                "revision": mission.revision + 1,
                "updated_at": now,
            }
        )
    )
    event = await publisher.publish(
        EventType.MISSION_UPDATED,
        {
            "mission_id": mission_id,
            "status": outcome.value,
            "outstanding_task_ids": [
                task.task_id for task in required if task.status is not TaskStatus.COMPLETED
            ],
            "unmet_criteria": [result.criterion for result in criterion_results if not result.met],
        },
        causation_id=last_event_id,
    )
    return event.event_id


def _authorization_evidence_matches(task: MissionTask, item: EvidenceRecord) -> bool:
    """Require the measured comparison from this Task or its acquisition dependency."""
    return (
        item.source_type is EvidenceSourceType.TOOL
        and item.metadata.get("success") is True
        and item.source_id == "web.http_compare_sessions"
        and item.metadata.get("mission_task_id")
        in {str(task.task_id), *(str(dep) for dep in task.dependency_task_ids)}
        and item.metadata.get("target_node_id")
        in {str(node_id) for node_id in task.related_attack_graph_node_ids}
        and _conclusive_session_comparison(item.metadata.get("session_comparison"))
    )


def _evaluate_criterion(
    criterion: str,
    tasks: list[MissionTask],
    evidence: list[EvidenceRecord],
) -> MissionCriterionResult:
    """Evaluate one mission success string. Unknown conditions never pass.

    Any criterion without a supported evaluator (wrong prefix or unknown
    workstream) returns met=False so it can never read as satisfied.
    Caller signature is unchanged.
    """
    prefix = "verified evidence for "
    if not criterion.startswith(prefix):
        return MissionCriterionResult(
            criterion=criterion, met=False, reason="criterion has no supported evaluator"
        )
    focus = criterion[len(prefix) :].lower()
    capability = (
        "recon"
        if focus == "reconnaissance"
        else "mobile"
        if focus == "mobile"
        else "web"
        if focus in {"web", "api", "web/api assessment", "authentication"}
        else None
    )
    if focus == "authorization":
        matching = [
            task for task in tasks if task.provenance.source == "replan:object_bound_endpoint"
        ]
    elif capability is not None:
        matching = [
            task
            for task in tasks
            if task.required_capability == capability
            and task.provenance.source == "engagement:bootstrap"
        ]
    else:
        return MissionCriterionResult(criterion=criterion, met=False, reason="unknown workstream")
    completed = [task for task in matching if task.status is TaskStatus.COMPLETED]
    if not completed:
        return MissionCriterionResult(
            criterion=criterion,
            met=False,
            task_ids=[task.task_id for task in matching],
            reason="no matching Task completed",
        )
    qualifying: set[str] = set()
    supported_tasks: set[TaskId] = set()
    for task in completed:
        for item in evidence:
            if (
                item.source_type is not EvidenceSourceType.TOOL
                or item.metadata.get("success") is not True
            ):
                continue
            if focus == "authorization":
                if not _authorization_evidence_matches(task, item):
                    continue
            elif item.metadata.get("mission_task_id") != str(task.task_id) or item.source_id.split(
                ".", 1
            )[0] not in ({"web", "api"} if capability == "web" else {capability}):
                continue
            if focus == "authentication":
                observation = item.metadata.get("http_observation")
                if not (
                    isinstance(observation, dict)
                    and isinstance(observation.get("session_ref"), str)
                    and type(observation.get("status_code")) is int
                    and observation["status_code"] in {*range(200, 300), 401, 403}
                ):
                    continue
            qualifying.add(str(item.evidence_id))
            supported_tasks.add(task.task_id)
    expected = [task for task in matching if task.required or task.status is TaskStatus.COMPLETED]
    met = bool(expected) and all(task.task_id in supported_tasks for task in expected)
    return MissionCriterionResult(
        criterion=criterion,
        met=met,
        task_ids=[task.task_id for task in expected],
        evidence_ids=sorted(qualifying),
        reason=None if met else "matching Task has no successful Evidence",
    )
