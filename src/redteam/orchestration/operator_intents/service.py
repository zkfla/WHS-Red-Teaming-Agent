"""``OperatorIntentService`` — the runtime Operator Intent channel.

This is the bridge between "an operator said something" and "the Task
Graph and Mission changed", and nothing more:

- **receive** turns redacted free text into a validated ``OperatorIntent``
  and records it (``OperatorInstructionReceived``).
- **apply** performs the *bounded* effect and records it
  (``OperatorIntentApplied``); every task it re-ranked or cancelled
  publishes its own event citing the applied event as ``causation_id``.
- **process_pending** is what OBSERVE calls each turn: it drains intents
  that arrived out-of-band and lets the run react before it schedules
  anything.
- **status** / **explain** are strictly read-only projections.

Hard boundaries:

- No tool is ever invoked. This module imports no gateway/capability and
  has no execution path; it only reads/updates the Mission and Task Graph
  through the ``core.ports`` Protocol.
- No scope expansion. FOCUS/EXCLUDE can only re-rank or drop *existing*
  work; they never create a target or touch the Engagement scope.
- No raw secret persistence. Instructions and parameters are redacted
  before the ``OperatorIntent`` exists; events carry ids and outcomes,
  never the instruction text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from redteam.attack_graph.identity import normalize_path
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.mission import MissionTask, OperatorIntent
from redteam.core.enums import GraphNodeType, MissionStatus, OperatorIntentType, TaskStatus
from redteam.core.ids import EventId, MissionId, OperatorIntentId, RunId, TaskId
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.events import CANDIDATE_REJECTED
from redteam.orchestration.operator_intents.interpreter import (
    classify_instruction,
    interpret_instruction,
)
from redteam.orchestration.operator_intents.models import (
    ActionExplanation,
    BlockedTaskExplanation,
    CandidateExplanation,
    IntentApplication,
    IntentDrainResult,
    IntentExplanation,
    IntentInterpretationError,
    IntentRecord,
    IntentStatus,
    IntentStatusEntry,
    IntentStatusReport,
)
from redteam.orchestration.operator_intents.redaction import redact_text
from redteam.orchestration.operator_intents.store import OperatorIntentStore
from redteam.orchestration.operator_intents.suppression import endpoint_excluded
from redteam.orchestration.task_graph.rules import TaskGraphError
from redteam.orchestration.work.models import ACTIVE_STATUSES, WorkStatus

#: Focused work is raised to this priority so it outranks bootstrap
#: (tens) and dynamic replan tasks (100). A stable target, so focusing the
#: same area twice is a no-op rather than an unbounded boost.
FOCUS_PRIORITY = 500

_TERMINAL_TASK_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
_CANCELLABLE_TASK_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED})
_QUERY_TYPES = frozenset({OperatorIntentType.EXPLANATION_QUERY, OperatorIntentType.STATUS_QUERY})

#: Every structured explanation list is bounded, so a long run cannot turn a
#: read-only answer into an unbounded projection.
_MAX_EXPLAIN_ITEMS = 5
_MAX_ENDPOINT_MATCHES = 5
#: Ceiling on each recorded suppression list so a long run cannot grow
#: ``mission.operator_preferences`` without bound. The newest entries win.
_MAX_PREFERENCE_ENTRIES = 100
#: Ceiling on the numeric scheduler-score components exposed in an explanation.
_MAX_PRIORITY_COMPONENTS = 20


def _as_score(value: object) -> float | None:
    """Coerce a scheduler score to a float without trusting its shape."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _bounded_components(value: object) -> dict[str, float]:
    """Keep only the numeric scheduler-score components, bounded and stable."""
    if not isinstance(value, dict):
        return {}
    components: dict[str, float] = {}
    for key in sorted(value):
        score = _as_score(value[key])
        if score is None:
            continue
        components[str(key)] = score
        if len(components) >= _MAX_PRIORITY_COMPONENTS:
            break
    return components


def _normalized_selector(selector: str) -> str:
    """Canonical form for comparing an operator selector to graph/task text."""
    value = str(selector).strip()
    if not value or value.startswith(("http://", "https://")):
        return value.lower()
    try:
        return normalize_path(value).lower()
    except Exception:  # noqa: BLE001 - a selector is untrusted input
        return value.lower()


def _selector_matches_text(selector: str, text: str) -> bool:
    """Whether a bounded selector names the endpoint/task described by ``text``."""
    if not selector or not text:
        return False
    lowered = text.lower()
    boundary = r"(?![A-Za-z0-9_./%-])"
    if re.search(re.escape(str(selector).strip().lower()) + boundary, lowered):
        return True
    normalized = _normalized_selector(selector)
    return bool(normalized) and bool(re.search(re.escape(normalized) + boundary, lowered))


def _task_haystack(task: MissionTask) -> str:
    return " ".join(
        [
            task.required_capability or "",
            task.title,
            task.objective,
            task.provenance.source,
            " ".join(str(node_id) for node_id in task.related_attack_graph_node_ids),
            " ".join(str(dep) for dep in task.dependency_task_ids),
        ]
    ).lower()


def _matches(
    task: MissionTask,
    areas: list[str],
    task_ids: list[TaskId],
    *,
    endpoint_selectors: list[str] | None = None,
    task_selectors: list[str] | None = None,
) -> bool:
    """Whether ``task`` is named by explicit ids, canonical areas or selectors.

    Endpoint and task selectors are *bounded strings* matched only against
    work that already exists — never a way to address a new target.
    """
    if task_ids and task.task_id in set(task_ids):
        return True
    haystack = _task_haystack(task)
    selectors = [str(item).strip() for item in (task_selectors or []) if str(item).strip()]
    if selectors:
        identifiers = {str(task.task_id).lower(), task.title.lower()}
        if any(selector.lower() in identifiers for selector in selectors):
            return True
    if areas:
        wanted = [str(area).strip().lower() for area in areas if str(area).strip()]
        if any(area in haystack for area in wanted):
            return True
    endpoints = [str(item).strip() for item in (endpoint_selectors or []) if str(item).strip()]
    if endpoints:
        if task.related_attack_graph_node_ids:
            return any(
                endpoint_excluded(node_id, endpoints)
                for node_id in task.related_attack_graph_node_ids
            )
        if any(_selector_matches_text(item, haystack) for item in endpoints):
            return True
    return False


def _excluded_preferences(mission) -> tuple[list[str], list[str], list[str]]:
    """Operator's recorded suppressions: (areas, endpoints, task ids)."""
    preferences = mission.operator_preferences or {}
    areas = [str(item) for item in (preferences.get("excluded_areas") or [])]
    endpoints = [str(item) for item in (preferences.get("excluded_endpoints") or [])]
    task_ids = [str(item) for item in (preferences.get("excluded_task_ids") or [])]
    return areas, endpoints, task_ids


def _is_suppressed(task: MissionTask, mission) -> bool:
    """Whether the operator has already excluded this Task (area/endpoint/id)."""
    areas, endpoints, task_ids = _excluded_preferences(mission)
    if not (areas or endpoints or task_ids):
        return False
    return _matches(
        task,
        areas,
        [],
        endpoint_selectors=endpoints,
        task_selectors=task_ids,
    )


def _partition_matches(
    mission,
    active: list[MissionTask],
    areas: list[str],
    task_ids: list[TaskId],
    endpoint_selectors: list[str],
    task_selectors: list[str],
) -> tuple[list[MissionTask], list[MissionTask]]:
    """Split matching Tasks into (allowed, already-suppressed).

    This is the service-side enforcement of operator suppression: a Task the
    operator already excluded is never silently re-raised by a later FOCUS.
    """
    matched: list[MissionTask] = []
    suppressed: list[MissionTask] = []
    for task in active:
        named = _matches(
            task,
            areas,
            task_ids,
            endpoint_selectors=endpoint_selectors,
            task_selectors=task_selectors,
        )
        if not named:
            continue
        if _is_suppressed(task, mission):
            suppressed.append(task)
        else:
            matched.append(task)
    return matched, suppressed


def _intent_selectors(intent: OperatorIntent, key: str) -> list[str]:
    return [str(item) for item in (intent.parameters.get(key) or []) if str(item).strip()]


@dataclass
class _PolicyOutcome:
    """One action's policy verdict, redacted reasons and evidence reference."""

    verdict: str
    reasons: list[str] = field(default_factory=list)
    evidence_event_id: EventId | None = None


@dataclass
class _MutationResult:
    outcome: str
    affected: list[TaskId] = field(default_factory=list)
    priority_changes: list[tuple[TaskId, int, int]] = field(default_factory=list)
    cancelled: list[TaskId] = field(default_factory=list)
    paused: bool = False
    stopped: bool = False


class OperatorIntentService:
    """Receive, apply and explain operator intents for one runtime."""

    def __init__(
        self,
        *,
        store: OperatorIntentStore,
        task_graph_store: MissionTaskStore,
        event_bus: object | None = None,
        graph_store: object | None = None,
        work_store: object | None = None,
    ) -> None:
        self._store = store
        self._tasks = task_graph_store
        self._event_bus = event_bus
        # Optional, read-only evidence sources for ``explain``. Absent in a
        # bare/legacy deps bundle, in which case the projection degrades to
        # Task-only evidence rather than failing.
        self._graph_store = graph_store
        self._work_store = work_store

    @classmethod
    def from_deps(cls, deps: object) -> OperatorIntentService | None:
        """Build from ``RuntimeDeps`` when an intent store is wired.

        ``None`` — not an exception — keeps every legacy run (unit tests,
        bare ``build_runtime_deps``, a checkpoint without the field)
        unchanged: OBSERVE simply skips the channel.
        """
        store = getattr(deps, "operator_intent_store", None)
        task_graph_store = getattr(deps, "task_graph_store", None)
        if store is None or task_graph_store is None:
            return None
        return cls(
            store=store,
            task_graph_store=task_graph_store,
            event_bus=getattr(deps, "event_bus", None),
            graph_store=getattr(deps, "graph_store", None),
            work_store=getattr(deps, "work_store", None),
        )

    # -- receive --------------------------------------------------------

    async def receive(
        self,
        instruction: str,
        *,
        mission_id: MissionId,
        run_id: RunId | None = None,
        task_ids: list[TaskId] | None = None,
        parameters: dict[str, object] | None = None,
        publisher: EventPublisher | None = None,
        last_event_id: EventId | None = None,
        now: datetime | None = None,
        source: str = "operator",
    ) -> IntentApplication:
        """Interpret and persist one operator instruction."""
        received_at = now or datetime.now(timezone.utc)
        try:
            intent = interpret_instruction(
                instruction,
                mission_id=mission_id,
                now=received_at,
                task_ids=task_ids,
                parameters=parameters,
                source=source,
            )
        except IntentInterpretationError as exc:
            return IntentApplication(
                status=IntentStatus.REJECTED,
                applied=False,
                outcome="rejected",
                reason=str(exc),
            )
        except ValidationError:
            return IntentApplication(
                status=IntentStatus.REJECTED,
                applied=False,
                outcome="rejected",
                reason="instruction contained an executable or unsafe parameter",
            )

        correlation_id = str(intent.intent_id)
        record = IntentRecord(
            intent=intent,
            status=IntentStatus.RECEIVED,
            run_id=run_id,
            correlation_id=correlation_id,
        )
        await self._store.append(record)

        received_event_id = await self._emit(
            publisher,
            EventType.OPERATOR_INSTRUCTION_RECEIVED,
            {
                "intent_id": intent.intent_id,
                "intent_type": intent.intent_type.value,
                "mission_id": mission_id,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        if received_event_id is not None:
            record = record.model_copy(update={"received_event_id": received_event_id})
            await self._store.update(record)

        return IntentApplication(
            status=IntentStatus.RECEIVED,
            applied=False,
            outcome="received",
            intent_id=intent.intent_id,
            intent_type=intent.intent_type,
            received_event_id=received_event_id,
        )

    async def receive_and_apply(
        self,
        instruction: str,
        *,
        mission_id: MissionId,
        run_id: RunId | None = None,
        task_ids: list[TaskId] | None = None,
        parameters: dict[str, object] | None = None,
        publisher: EventPublisher | None = None,
        last_event_id: EventId | None = None,
        now: datetime | None = None,
        source: str = "operator",
    ) -> IntentApplication:
        """``receive`` then ``apply`` — the CLI's one-shot entry point."""
        received = await self.receive(
            instruction,
            mission_id=mission_id,
            run_id=run_id,
            task_ids=task_ids,
            parameters=parameters,
            publisher=publisher,
            last_event_id=last_event_id,
            now=now,
            source=source,
        )
        if received.intent_id is None:
            return received
        return await self.apply(
            received.intent_id,
            publisher=publisher,
            last_event_id=received.received_event_id or last_event_id,
            run_id=run_id,
            now=now,
        )

    # -- apply ----------------------------------------------------------

    async def apply(
        self,
        intent: OperatorIntentId | OperatorIntent | IntentRecord,
        *,
        publisher: EventPublisher | None = None,
        last_event_id: EventId | None = None,
        run_id: RunId | None = None,
        now: datetime | None = None,
    ) -> IntentApplication:
        """Apply a previously received intent exactly once."""
        record = await self._resolve(intent)
        if record is None:
            return IntentApplication(
                status=IntentStatus.REJECTED,
                applied=False,
                outcome="not_found",
                reason="intent not found",
            )
        if record.status is IntentStatus.APPLIED:
            return self._application(record, applied=True)
        if record.status is IntentStatus.REJECTED:
            return self._application(record, applied=False)
        if run_id is not None and record.run_id is None:
            record = record.model_copy(update={"run_id": run_id})
        application, _ = await self._apply_record(
            record, publisher=publisher, last_event_id=last_event_id, now=now
        )
        return application

    async def process_pending(
        self,
        *,
        mission_id: MissionId,
        run_id: RunId | None = None,
        publisher: EventPublisher | None = None,
        last_event_id: EventId | None = None,
        now: datetime | None = None,
    ) -> IntentDrainResult:
        """Apply every ``received`` intent for a mission, oldest first."""
        records = await self._store.list_for_mission(mission_id, status=IntentStatus.RECEIVED)
        applications: list[IntentApplication] = []
        current_event = last_event_id
        for record in records:
            if record.run_id is None and run_id is not None:
                record = record.model_copy(update={"run_id": run_id})
            if record.received_event_id is None:
                received_event_id = await self._emit(
                    publisher,
                    EventType.OPERATOR_INSTRUCTION_RECEIVED,
                    {
                        "intent_id": record.intent.intent_id,
                        "intent_type": record.intent.intent_type.value,
                        "mission_id": mission_id,
                    },
                    correlation_id=record.correlation_id,
                    causation_id=current_event,
                )
                if received_event_id is not None:
                    record = record.model_copy(update={"received_event_id": received_event_id})
                    await self._store.update(record)
                    current_event = received_event_id
            application, current_event = await self._apply_record(
                record, publisher=publisher, last_event_id=current_event, now=now
            )
            applications.append(application)

        mission = await self._tasks.get_mission(mission_id)
        status = mission.status if mission is not None else None
        return IntentDrainResult(
            last_event_id=current_event,
            applications=applications,
            last_intent_id=applications[-1].intent_id if applications else None,
            paused=status is MissionStatus.PAUSED,
            stopped=status is MissionStatus.CANCELLED,
            mission_status=status,
        )

    async def _apply_record(
        self,
        record: IntentRecord,
        *,
        publisher: EventPublisher | None,
        last_event_id: EventId | None,
        now: datetime | None,
    ) -> tuple[IntentApplication, EventId | None]:
        applied_at = now or datetime.now(timezone.utc)
        try:
            mutation = await self._mutate(record.intent, now=applied_at)
        except (TaskGraphError, ValueError) as exc:
            # ValueError may echo a caller-supplied parameter (e.g. int() of
            # an invalid priority). Keyword-based redaction cannot recognize
            # an unlabelled credential in that exception text.
            rejection_reason = f"intent_validation_failed:{type(exc).__name__}"
            rejected = record.model_copy(
                update={
                    "status": IntentStatus.REJECTED,
                    "reason": rejection_reason,
                    "applied_at": applied_at,
                }
            )
            await self._store.update(rejected)
            current_event = await self._emit(
                publisher,
                EventType.OPERATOR_INTENT_REJECTED,
                {"intent_id": record.intent.intent_id, "reason": rejection_reason},
                correlation_id=record.correlation_id,
                causation_id=record.received_event_id or last_event_id,
            )
            return (
                IntentApplication(
                    status=IntentStatus.REJECTED,
                    applied=False,
                    outcome="rejected",
                    intent_id=record.intent.intent_id,
                    intent_type=record.intent.intent_type,
                    reason=rejection_reason,
                ),
                current_event or last_event_id,
            )

        applied_event_id = await self._emit(
            publisher,
            EventType.OPERATOR_INTENT_APPLIED,
            {
                "intent_id": record.intent.intent_id,
                "intent_type": record.intent.intent_type.value,
                "outcome": mutation.outcome,
                "affected_task_ids": mutation.affected,
            },
            correlation_id=record.correlation_id,
            causation_id=record.received_event_id or last_event_id,
        )
        current_event = applied_event_id or last_event_id

        for task_id, previous, new in mutation.priority_changes:
            event_id = await self._emit(
                publisher,
                EventType.TASK_PRIORITY_CHANGED,
                {
                    "task_id": task_id,
                    "intent_id": record.intent.intent_id,
                    "previous_priority": previous,
                    "priority": new,
                },
                correlation_id=record.correlation_id,
                causation_id=applied_event_id,
            )
            current_event = event_id or current_event
        for task_id in mutation.cancelled:
            event_id = await self._emit(
                publisher,
                EventType.TASK_CANCELLED,
                {"task_id": task_id, "intent_id": record.intent.intent_id},
                correlation_id=record.correlation_id,
                causation_id=applied_event_id,
            )
            current_event = event_id or current_event

        applied = record.model_copy(
            update={
                "status": IntentStatus.APPLIED,
                "applied_event_id": applied_event_id,
                "applied_at": applied_at,
                "outcome": mutation.outcome,
                "affected_task_ids": mutation.affected,
                "reason": None,
            }
        )
        await self._store.update(applied)
        return self._application(applied, applied=True), current_event

    def _application(self, record: IntentRecord, *, applied: bool) -> IntentApplication:
        return IntentApplication(
            status=record.status,
            applied=applied,
            outcome=record.outcome or record.status.value,
            intent_id=record.intent.intent_id,
            intent_type=record.intent.intent_type,
            affected_task_ids=list(record.affected_task_ids),
            reason=record.reason,
            received_event_id=record.received_event_id,
            applied_event_id=record.applied_event_id,
        )

    # -- read-only projections -----------------------------------------

    async def status(self, *, mission_id: MissionId) -> IntentStatusReport:
        """Read-only status of a mission's operator-intent channel."""
        mission = await self._tasks.get_mission(mission_id)
        if mission is None:
            raise ValueError(f"mission not found: {mission_id}")
        tasks = await self._tasks.list_tasks(mission_id=mission_id)
        records = await self._store.list_for_mission(mission_id)

        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        preferences = mission.operator_preferences or {}
        focus_areas = [str(item) for item in preferences.get("focus_areas", [])]
        excluded_areas = [str(item) for item in preferences.get("excluded_areas", [])]
        excluded_endpoints = [str(item) for item in preferences.get("excluded_endpoints", [])]
        blocked_tasks = self._blocked_explanations(tasks)
        recent = [self._entry(record) for record in records[-10:]]
        paused = mission.status is MissionStatus.PAUSED
        applied_count = sum(1 for record in records if record.status is IntentStatus.APPLIED)
        summary = (
            f"mission {mission.status.value}; {len(tasks)} task(s); "
            f"{applied_count} intent(s) applied" + (", paused" if paused else "")
        )
        if blocked_tasks:
            summary += (
                f"; {len(blocked_tasks)} blocked task(s); "
                f"first: {blocked_tasks[0].task_id} ({blocked_tasks[0].blocking_reason})"
            )
        if excluded_endpoints:
            summary += f"; excluded endpoint(s): {', '.join(excluded_endpoints[:3])}"
        return IntentStatusReport(
            mission_id=mission_id,
            mission_status=mission.status,
            active_task_id=next(
                (task.task_id for task in tasks if task.status is TaskStatus.RUNNING), None
            ),
            task_count=len(tasks),
            task_status_counts=counts,
            paused=paused,
            focus_areas=focus_areas,
            excluded_areas=excluded_areas,
            excluded_endpoints=excluded_endpoints,
            blocked_tasks=blocked_tasks,
            recent_intents=recent,
            summary=summary,
        )

    async def explain(
        self,
        *,
        mission_id: MissionId,
        run_id: RunId | None = None,
        intent_id: OperatorIntentId | None = None,
        task_ids: list[TaskId] | None = None,
        endpoint_selectors: list[str] | None = None,
        subjects: list[str] | None = None,
    ) -> IntentExplanation:
        """Read-only explanation of the mission's current position.

        Structured, not just prose: the explanation is assembled from the
        Task Graph (blocked tasks and their still-open dependencies), the
        Attack Graph (which endpoint a selector names), the Work ledger and
        the Event Bus (why a candidate was dropped, which action was chosen)
        when those deps are wired. It never writes and never reaches a tool.

        When the caller does not pass ``endpoint_selectors``/``subjects``,
        they are taken from the most recent explanation/status intent for the
        mission — so the console's "why /api/docs 안 봐?" answers about
        ``/api/docs`` without a call-site change.
        """
        mission = await self._tasks.get_mission(mission_id)
        if mission is None:
            raise ValueError(f"mission not found: {mission_id}")
        tasks = await self._tasks.list_tasks(mission_id=mission_id)
        records = await self._store.list_for_mission(mission_id)
        if run_id is not None:
            records = [record for record in records if record.run_id == run_id]
        if intent_id is not None:
            records = [record for record in records if record.intent.intent_id == intent_id]

        if endpoint_selectors is None or subjects is None:
            source = self._latest_query_intent(records, intent_id=intent_id)
            if source is not None:
                if endpoint_selectors is None:
                    endpoint_selectors = [
                        str(item) for item in source.parameters.get("endpoint_selectors", [])
                    ]
                if subjects is None:
                    subjects = [str(item) for item in source.parameters.get("subjects", [])]
                if not task_ids:
                    derived = [str(item) for item in source.parameters.get("task_selectors", [])]
                    if derived:
                        task_ids = [TaskId(item) for item in derived]
        endpoint_selectors = [str(item) for item in (endpoint_selectors or [])]
        subjects = [str(item) for item in (subjects or [])]
        all_tasks = tasks
        if task_ids:
            wanted = set(task_ids)
            tasks = [task for task in tasks if task.task_id in wanted]

        active = next((task for task in tasks if task.status is TaskStatus.RUNNING), None)
        priorities = {
            str(task.task_id): task.priority
            for task in sorted(tasks, key=lambda item: (-item.priority, str(item.task_id)))
        }
        paused = mission.status is MissionStatus.PAUSED
        if active is not None:
            summary = (
                f"mission {mission.status.value}; active task '{active.title}' is "
                f"{active.status.value}"
            )
        else:
            summary = f"mission {mission.status.value}; no task is currently running"
        if paused:
            summary += " (paused by operator intent; the active task is preserved for resume)"

        excluded_areas, excluded_endpoints, _excluded_task_ids = _excluded_preferences(mission)
        blocked_tasks = self._blocked_explanations(tasks, lookup=all_tasks)
        endpoint_matches = await self._endpoint_matches(endpoint_selectors)
        excluded_text = " ".join(excluded_endpoints)
        suppressed_selectors = [
            selector
            for selector in endpoint_selectors
            if excluded_text and _selector_matches_text(selector, excluded_text)
        ]
        rejected_candidates = self._rejected_candidates(run_id, mission.engagement_id)
        chosen_actions = self._chosen_actions(run_id, mission.engagement_id)
        if not (rejected_candidates and chosen_actions):
            work_rejected, work_chosen = await self._work_evidence(run_id)
            rejected_candidates = rejected_candidates or work_rejected
            chosen_actions = chosen_actions or work_chosen

        # Structured evidence first: an endpoint question and a blocked-task
        # question are the two the operator most needs answered precisely.
        if endpoint_selectors:
            shown = ", ".join(endpoint_selectors[:_MAX_EXPLAIN_ITEMS])
            if suppressed_selectors:
                summary += f"; endpoint {shown} is excluded by operator intent"
            elif endpoint_matches:
                summary += f"; endpoint {shown} is known as {endpoint_matches[0]}"
            else:
                summary += f"; endpoint {shown} is not recorded in the attack graph"
        if blocked_tasks:
            blocked_top = blocked_tasks[0]
            summary += f"; blocked: {blocked_top.task_id} ({blocked_top.blocking_reason})"
        if "candidate" in subjects and rejected_candidates:
            candidate_top = rejected_candidates[0]
            summary += (
                f"; candidate {candidate_top.operation} rejected: "
                f"{candidate_top.reason or 'unspecified'}"
            )
        if "action" in subjects and chosen_actions:
            action_top = chosen_actions[0]
            detail = action_top.status or "proposed"
            if action_top.priority is not None:
                detail += f", score {action_top.priority:g}"
            summary += f"; action {action_top.operation} ({detail})"
            if action_top.policy_reasons:
                summary += f" policy: {action_top.policy_reasons[0]}"
        if records:
            latest = records[-1]
            summary += (
                f"; last intent {latest.intent.intent_type.value} "
                f"({latest.status.value}: {latest.outcome or 'no outcome'})"
            )

        return IntentExplanation(
            mission_id=mission_id,
            intent_id=intent_id,
            summary=summary,
            mission_status=mission.status,
            active_task_id=active.task_id if active else None,
            active_task_title=active.title if active else None,
            paused=paused,
            task_priorities=priorities,
            subjects=subjects,
            endpoint_selectors=endpoint_selectors,
            endpoint_matches=endpoint_matches,
            excluded_areas=excluded_areas,
            excluded_endpoints=excluded_endpoints,
            suppressed_selectors=list(dict.fromkeys(suppressed_selectors)),
            blocked_tasks=blocked_tasks,
            rejected_candidates=rejected_candidates,
            chosen_actions=chosen_actions,
            recent_intents=[self._entry(record) for record in records[-10:]],
        )

    # -- structured explanation helpers ---------------------------------

    @staticmethod
    def _latest_query_intent(
        records: list[IntentRecord], *, intent_id: OperatorIntentId | None
    ) -> OperatorIntent | None:
        if intent_id is not None:
            for record in records:
                if record.intent.intent_id == intent_id:
                    return record.intent
            return None
        for record in reversed(records):
            if record.intent.intent_type in _QUERY_TYPES:
                return record.intent
        return None

    @staticmethod
    def _blocked_explanations(
        tasks: list[MissionTask], *, lookup: list[MissionTask] | None = None
    ) -> list[BlockedTaskExplanation]:
        by_id = {task.task_id: task for task in (lookup if lookup is not None else tasks)}
        explanations: list[BlockedTaskExplanation] = []
        for task in tasks:
            if task.status is not TaskStatus.BLOCKED:
                continue
            blocked_dependencies = sorted(
                (
                    dependency
                    for dependency in task.dependency_task_ids
                    if dependency not in by_id
                    or by_id[dependency].status not in _TERMINAL_TASK_STATUSES
                ),
                key=str,
            )
            explanations.append(
                BlockedTaskExplanation(
                    task_id=task.task_id,
                    title=task.title,
                    blocking_reason=redact_text(task.blocking_reason or "unspecified"),
                    blocked_dependency_ids=blocked_dependencies,
                )
            )
        explanations.sort(key=lambda item: str(item.task_id))
        return explanations[:_MAX_EXPLAIN_ITEMS]

    async def _endpoint_matches(self, selectors: list[str]) -> list[str]:
        """Attack-Graph endpoint ids/labels named by ``selectors`` (bounded)."""
        if not selectors or self._graph_store is None:
            return []
        list_nodes = getattr(self._graph_store, "list_nodes", None)
        if not callable(list_nodes):
            return []
        try:
            nodes = await list_nodes()
        except Exception:  # noqa: BLE001 - a non-listable store degrades to no matches
            return []
        matches: list[str] = []
        for node in nodes:
            if getattr(node, "type", None) is not GraphNodeType.ENDPOINT:
                continue
            reference = str(node.id)
            if any(_selector_matches_text(selector, reference) for selector in selectors):
                if reference not in matches:
                    matches.append(reference)
            if len(matches) >= _MAX_ENDPOINT_MATCHES:
                break
        return matches

    def _rejected_candidates(
        self, run_id: RunId | None, engagement_id: str
    ) -> list[CandidateExplanation]:
        explanations: list[CandidateExplanation] = []
        for event in self._event_history(CANDIDATE_REJECTED, run_id, engagement_id):
            payload = event.payload or {}
            operation = str(payload.get("operation") or "unknown")
            candidate_key = payload.get("candidate_key")
            explanations.append(
                CandidateExplanation(
                    operation=operation,
                    reason=(
                        redact_text(str(payload.get("reason")))
                        if payload.get("reason") is not None else None
                    ),
                    field=(str(payload.get("field")) if payload.get("field") else None),
                    candidate_key=(str(candidate_key) if candidate_key else None),
                    evidence_event_id=event.event_id,
                )
            )
        return explanations[-_MAX_EXPLAIN_ITEMS:]

    def _chosen_actions(
        self, run_id: RunId | None, engagement_id: str
    ) -> list[ActionExplanation]:
        """Proposed actions joined with their policy outcome by ``action_id``.

        The proposal is the source of the candidate score (``priority``); the
        ``ActionApproved``/``ActionDenied`` events are the source of the
        verdict and (redacted) reasons; ``PolicyEvaluated`` adds the matched
        rule ids. All reads are run/engagement filtered via ``_event_history``
        and every list is bounded, so a shared bus cannot leak another run.
        """
        policy_by_action: dict[str, _PolicyOutcome] = {}
        for event_type, verdict in (
            (EventType.ACTION_APPROVED, "allow"),
            (EventType.ACTION_DENIED, "deny"),
            (EventType.APPROVAL_REQUIRED, "require_approval"),
        ):
            for event in self._event_history(event_type, run_id, engagement_id):
                payload = event.payload or {}
                action_id = payload.get("action_id")
                if not action_id:
                    continue
                policy_by_action[str(action_id)] = _PolicyOutcome(
                    verdict=verdict,
                    reasons=[
                        redact_text(str(reason))
                        for reason in (payload.get("reasons") or [])[:_MAX_EXPLAIN_ITEMS]
                    ],
                    evidence_event_id=event.event_id,
                )

        rules_by_action: dict[str, list[str]] = {}
        for event in self._event_history(EventType.POLICY_EVALUATED, run_id, engagement_id):
            payload = event.payload or {}
            action_id = payload.get("action_id")
            if action_id:
                rules_by_action[str(action_id)] = [
                    str(rule) for rule in (payload.get("matched_rules") or [])[:_MAX_EXPLAIN_ITEMS]
                ]

        explanations: list[ActionExplanation] = []
        for event in self._event_history(EventType.ACTION_PROPOSED, run_id, engagement_id):
            payload = event.payload or {}
            action_id = payload.get("action_id")
            action_key = str(action_id) if action_id else ""
            policy = policy_by_action.get(action_key)
            policy_verdict = policy.verdict if policy else None
            explanations.append(
                ActionExplanation(
                    operation=str(payload.get("operation") or "unknown"),
                    candidate_key=(
                        str(payload["candidate_key"]) if payload.get("candidate_key") else None
                    ),
                    target=(str(payload["target"]) if payload.get("target") else None),
                    status=(policy_verdict or "proposed"),
                    action_id=(action_key or None),
                    work_id=(str(payload["work_id"]) if payload.get("work_id") else None),
                    priority=_as_score(payload.get("priority")),
                    priority_components=_bounded_components(payload.get("priority_components")),
                    policy_verdict=policy_verdict,
                    policy_reasons=(list(policy.reasons) if policy else []),
                    matched_rules=list(rules_by_action.get(action_key, [])),
                    evidence_event_id=(policy.evidence_event_id if policy else event.event_id),
                )
            )
        return explanations[-_MAX_EXPLAIN_ITEMS:]

    async def _work_evidence(
        self, run_id: RunId | None,
    ) -> tuple[list[CandidateExplanation], list[ActionExplanation]]:
        """Work-ledger fallback when the Event Bus cannot be introspected."""
        if run_id is None:
            return [], []
        list_items = getattr(self._work_store, "list_items", None)
        if not callable(list_items):
            return [], []
        try:
            items = list(await list_items(run_id=run_id))
        except Exception:  # noqa: BLE001 - optional evidence, never fatal
            return [], []
        rejected: list[CandidateExplanation] = []
        chosen: list[ActionExplanation] = []
        for item in items:
            candidate_key = getattr(item, "candidate_key", None) or "unknown"
            status = getattr(item, "status", None)
            status_value = getattr(status, "value", None) or (
                str(status) if status is not None else None
            )
            reason = getattr(item, "outcome_reason", None)
            work_id = getattr(item, "work_id", None)
            priority = _as_score(getattr(getattr(item, "priority", None), "total", None))
            if status is WorkStatus.SUCCEEDED or status in ACTIVE_STATUSES:
                chosen.append(
                    ActionExplanation(
                        operation=str(candidate_key),
                        candidate_key=str(candidate_key),
                        work_id=(str(work_id) if work_id else None),
                        priority=priority,
                        status=status_value,
                    )
                )
            else:
                rejected.append(
                    CandidateExplanation(
                        operation=str(candidate_key),
                        reason=redact_text(str(reason)) if reason else None,
                        candidate_key=str(candidate_key),
                        priority=priority,
                    )
                )
        return rejected[-_MAX_EXPLAIN_ITEMS:], chosen[-_MAX_EXPLAIN_ITEMS:]

    def _event_history(
        self, event_type: str, run_id: RunId | None, engagement_id: str
    ) -> list[EventEnvelope]:
        if run_id is None:
            return []  # no safe way to bind a shared bus to this Mission
        history = getattr(self._event_bus, "history", None)
        if not callable(history):
            return []
        try:
            events = history(event_type=event_type)
        except TypeError:
            try:
                events = [
                    event for event in history() if getattr(event, "event_type", None) == event_type
                ]
            except Exception:  # noqa: BLE001
                return []
        except Exception:  # noqa: BLE001
            return []
        return [
            event for event in events
            if str(event.engagement_id) == str(engagement_id)
            and (run_id is None or event.run_id == run_id)
        ][-_MAX_EXPLAIN_ITEMS:]

    # -- internals ------------------------------------------------------

    async def _resolve(
        self, intent: OperatorIntentId | OperatorIntent | IntentRecord
    ) -> IntentRecord | None:
        if isinstance(intent, IntentRecord):
            return intent
        intent_id = intent.intent_id if isinstance(intent, OperatorIntent) else intent
        return await self._store.get(intent_id)

    async def _emit(
        self,
        publisher: EventPublisher | None,
        event_type: str,
        payload: dict[str, object],
        *,
        correlation_id: str,
        causation_id: EventId | None,
    ) -> EventId | None:
        if publisher is None:
            return None
        event = await publisher.publish(
            event_type,
            payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return event.event_id

    @staticmethod
    def _entry(record: IntentRecord) -> IntentStatusEntry:
        return IntentStatusEntry(
            intent_id=record.intent.intent_id,
            intent_type=record.intent.intent_type,
            status=record.status,
            created_at=record.intent.created_at,
            outcome=record.outcome,
        )

    async def _mutate(self, intent: OperatorIntent, *, now: datetime) -> _MutationResult:
        mission = await self._tasks.get_mission(intent.mission_id)
        if mission is None:
            raise ValueError(f"mission not found: {intent.mission_id}")
        intent_type = intent.intent_type

        if intent_type is OperatorIntentType.PAUSE:
            return await self._pause(mission, now)
        if intent_type is OperatorIntentType.RESUME:
            return await self._resume(mission, now)
        if intent_type is OperatorIntentType.STOP:
            return await self._stop(mission, now)

        tasks = await self._tasks.list_tasks(mission_id=intent.mission_id)
        active = [task for task in tasks if task.status not in _TERMINAL_TASK_STATUSES]

        if intent_type is OperatorIntentType.FOCUS:
            return await self._focus(mission, intent, active, now)
        if intent_type is OperatorIntentType.REPRIORITIZE:
            return await self._reprioritize(mission, intent, active, now)
        if intent_type is OperatorIntentType.EXCLUDE:
            return await self._exclude(mission, intent, active, now)
        if intent_type in _QUERY_TYPES:
            return _MutationResult(outcome=f"answered {intent_type.value}")
        return _MutationResult(outcome=f"no-op for {intent_type.value}")

    async def _pause(self, mission, now: datetime) -> _MutationResult:
        if mission.status is MissionStatus.PAUSED:
            return _MutationResult(outcome="already_paused", paused=True)
        if mission.status is not MissionStatus.ACTIVE:
            raise ValueError(f"cannot pause mission in status {mission.status.value}")
        await self._set_mission_status(mission, MissionStatus.PAUSED, now)
        return _MutationResult(outcome="mission_paused", paused=True)

    async def _resume(self, mission, now: datetime) -> _MutationResult:
        if mission.status is MissionStatus.ACTIVE:
            return _MutationResult(outcome="already_active")
        if mission.status is not MissionStatus.PAUSED:
            raise ValueError(f"cannot resume mission in status {mission.status.value}")
        await self._set_mission_status(mission, MissionStatus.ACTIVE, now)
        return _MutationResult(outcome="mission_resumed")

    async def _stop(self, mission, now: datetime) -> _MutationResult:
        if mission.status not in {
            MissionStatus.COMPLETED,
            MissionStatus.FAILED,
            MissionStatus.CANCELLED,
        }:
            await self._set_mission_status(mission, MissionStatus.CANCELLED, now)
        cancelled: list[TaskId] = []
        for task in await self._tasks.list_tasks(mission_id=mission.mission_id):
            if task.status in _TERMINAL_TASK_STATUSES:
                continue
            await self._tasks.transition_task(task.task_id, TaskStatus.CANCELLED, now=now)
            cancelled.append(task.task_id)
        return _MutationResult(
            outcome="mission_stopped", affected=cancelled, cancelled=cancelled, stopped=True
        )

    async def _focus(
        self, mission, intent: OperatorIntent, active: list[MissionTask], now: datetime
    ) -> _MutationResult:
        areas = [str(area) for area in intent.parameters.get("focus_areas", [])]
        endpoints = _intent_selectors(intent, "endpoint_selectors")
        task_selectors = _intent_selectors(intent, "task_selectors")
        matched, suppressed = _partition_matches(
            mission, active, areas, intent.task_ids, endpoints, task_selectors
        )
        changes: list[tuple[TaskId, int, int]] = []
        for task in matched:
            new_priority = max(task.priority, FOCUS_PRIORITY)
            if new_priority != task.priority:
                await self._tasks.update_priority(task.task_id, new_priority, now=now)
                changes.append((task.task_id, task.priority, new_priority))
        preferences: dict[str, list[str]] = {}
        if areas:
            preferences["focus_areas"] = areas
        if endpoints:
            preferences["focus_endpoints"] = endpoints
        if preferences:
            await self._merge_preferences(mission, preferences, now)
        outcome = f"focused {len(matched)} task(s)"
        if suppressed:
            outcome += f"; {len(suppressed)} suppressed by operator exclusion"
        return _MutationResult(
            outcome=outcome,
            affected=[task.task_id for task in matched],
            priority_changes=changes,
        )

    async def _reprioritize(
        self, mission, intent: OperatorIntent, active: list[MissionTask], now: datetime
    ) -> _MutationResult:
        areas = [str(area) for area in intent.parameters.get("focus_areas", [])]
        endpoints = _intent_selectors(intent, "endpoint_selectors")
        task_selectors = _intent_selectors(intent, "task_selectors")
        matched, suppressed = _partition_matches(
            mission, active, areas, intent.task_ids, endpoints, task_selectors
        )
        mapping = intent.parameters.get("priorities")
        explicit = intent.parameters.get("priority")
        changes: list[tuple[TaskId, int, int]] = []
        for task in matched:
            if isinstance(mapping, dict) and str(task.task_id) in mapping:
                new_priority = int(mapping[str(task.task_id)])
            elif explicit is not None:
                new_priority = int(explicit)
            elif task.priority < FOCUS_PRIORITY:
                new_priority = FOCUS_PRIORITY
            else:
                continue
            if new_priority != task.priority:
                await self._tasks.update_priority(task.task_id, new_priority, now=now)
                changes.append((task.task_id, task.priority, new_priority))
        preferences: dict[str, list[str]] = {}
        if areas:
            preferences["focus_areas"] = areas
        if endpoints:
            preferences["focus_endpoints"] = endpoints
        if preferences:
            await self._merge_preferences(mission, preferences, now)
        outcome = f"reprioritized {len(changes)} task(s)"
        if suppressed:
            outcome += f"; {len(suppressed)} suppressed by operator exclusion"
        return _MutationResult(
            outcome=outcome,
            affected=[task_id for task_id, _, _ in changes],
            priority_changes=changes,
        )

    async def _exclude(
        self, mission, intent: OperatorIntent, active: list[MissionTask], now: datetime
    ) -> _MutationResult:
        areas = [str(area) for area in intent.parameters.get("exclude_areas", [])]
        endpoints = _intent_selectors(intent, "endpoint_selectors")
        task_selectors = _intent_selectors(intent, "task_selectors")
        matched = [
            task
            for task in active
            if _matches(
                task,
                areas,
                intent.task_ids,
                endpoint_selectors=endpoints,
                task_selectors=task_selectors,
            )
        ]
        cancelled: list[TaskId] = []
        for task in matched:
            if task.status not in _CANCELLABLE_TASK_STATUSES:
                # A RUNNING task may already be mid-execution; never yank it.
                continue
            await self._tasks.transition_task(task.task_id, TaskStatus.CANCELLED, now=now)
            cancelled.append(task.task_id)
        preferences: dict[str, list[str]] = {}
        if areas:
            preferences["excluded_areas"] = areas
        if endpoints:
            preferences["excluded_endpoints"] = endpoints
        # Persist every excluded identity so downstream candidate generation /
        # replanning can honour the suppression (the service records it; those
        # modules read ``mission.operator_preferences``).
        excluded_task_ids = [str(task_id) for task_id in intent.task_ids] + [
            str(task.task_id) for task in matched
        ]
        excluded_task_ids += task_selectors
        if excluded_task_ids:
            preferences["excluded_task_ids"] = list(dict.fromkeys(excluded_task_ids))
        if preferences:
            await self._merge_preferences(mission, preferences, now)
        outcome = f"excluded {len(cancelled)} task(s)"
        if endpoints:
            outcome += "; endpoint selector(s) recorded for suppression"
        return _MutationResult(
            outcome=outcome,
            affected=cancelled,
            cancelled=cancelled,
        )

    async def _set_mission_status(self, mission, status: MissionStatus, now: datetime) -> None:
        updated = mission.model_copy(
            update={"status": status, "revision": mission.revision + 1, "updated_at": now}
        )
        await self._tasks.update_mission(updated)

    async def _merge_preferences(
        self, mission, updates: dict[str, list[str]], now: datetime
    ) -> None:
        preferences = dict(mission.operator_preferences)
        for key, values in updates.items():
            existing = [str(item) for item in (preferences.get(key) or [])]
            merged = list(dict.fromkeys([*existing, *[str(item) for item in values]]))
            preferences[key] = merged[-_MAX_PREFERENCE_ENTRIES:]
        updated = mission.model_copy(
            update={
                "operator_preferences": preferences,
                "revision": mission.revision + 1,
                "updated_at": now,
            }
        )
        await self._tasks.update_mission(updated)


__all__ = ["FOCUS_PRIORITY", "OperatorIntentService", "classify_instruction"]
