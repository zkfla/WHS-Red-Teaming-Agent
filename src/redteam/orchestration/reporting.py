"""Read-only terminal projection for the Reporting role."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.enums import AgentPhase, MissionStatus, TaskStatus
from redteam.core.ids import ApprovalId, EvidenceId, FindingId, MissionId, RunId, TaskId
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.orchestration.context_projection import MappingStatus, project_run_processing
from redteam.orchestration.coverage import CoverageGapSummary, project_coverage_gaps
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.operator_intents.redaction import redact_text
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.work.store import WorkStore

if TYPE_CHECKING:
    from redteam.orchestration.agents.definition import AgentDefinition


class FindingProjection(BaseModel):
    finding_id: FindingId
    title: str
    severity: str
    verified: bool
    evidence_ids: list[str] = Field(default_factory=list)


#: Canonical workstream families. Ordered so the more specific labels win:
#: ``authorization`` must be matched before ``authentication`` (both contain
#: ``auth``), and both before the generic ``web`` surface.
_WORKSTREAM_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("authorization", ("authz", "authorization", "access control", "권한", "인가")),
    ("authentication", ("authn", "authentication", "login", "session", "인증", "로그인")),
    ("recon", ("recon", "reconnaissance", "discovery", "정찰", "탐색")),
    ("mobile", ("mobile", "android", "ios", "apk", "모바일")),
    ("web", ("web", "웹", "http", "api", "endpoint", "rest")),
)

_WORKSTREAM_LABELS = {
    "recon": "Reconnaissance",
    "web": "Web/API",
    "authentication": "Authentication",
    "authorization": "Authorization",
    "mobile": "Mobile",
}


def _workstream_key(text: str) -> str | None:
    """Map free text to a canonical workstream family, or ``None``."""
    lowered = text.lower()
    for key, needles in _WORKSTREAM_FAMILIES:
        if any(needle in lowered for needle in needles):
            return key
    return None


def _normalize_report_token(value: str) -> str:
    return value.strip().lower()


def _definition_group_key(definition: Any) -> str:
    return f"agent:{str(definition.agent_id)}"


def _index_agent_definitions(
    agent_definitions: Iterable[Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    by_agent_id: dict[str, Any] = {}
    by_task_token: dict[str, Any] = {}
    by_label_token: dict[str, Any] = {}
    for definition in agent_definitions:
        agent_id_text = str(definition.agent_id)
        if agent_id_text and agent_id_text not in by_agent_id:
            by_agent_id[agent_id_text] = definition
        lowered_agent_id = _normalize_report_token(agent_id_text)
        if lowered_agent_id and lowered_agent_id not in by_agent_id:
            by_agent_id[lowered_agent_id] = definition
        role_token = _normalize_report_token(str(getattr(definition, "role", "") or ""))
        if role_token and role_token not in by_task_token:
            by_task_token[role_token] = definition
        for capability in getattr(definition, "capabilities", None) or ():
            token = _normalize_report_token(str(capability or ""))
            if token and token not in by_task_token:
                by_task_token[token] = definition
        for label in (
            [str(getattr(definition, "role", "") or "")]
            + [str(item or "") for item in (getattr(definition, "capabilities", None) or ())]
            + [str(getattr(definition, "name", "") or "")]
        ):
            token = _normalize_report_token(label)
            if token and token not in by_label_token:
                by_label_token[token] = definition
    return by_agent_id, by_task_token, by_label_token


def _match_task_definition(
    task: MissionTask,
    by_agent_id: dict[str, Any],
    by_task_token: dict[str, Any],
) -> Any | None:
    assigned = getattr(task, "assigned_agent", None)
    if assigned is not None:
        assigned_text = str(assigned)
        matched = by_agent_id.get(assigned_text)
        if matched is not None:
            return matched
        matched = by_agent_id.get(_normalize_report_token(assigned_text))
        if matched is not None:
            return matched
    required = getattr(task, "required_capability", None)
    if required:
        matched = by_task_token.get(_normalize_report_token(str(required)))
        if matched is not None:
            return matched
    return None


def _match_workstream_definition(label: str, by_label_token: dict[str, Any]) -> Any | None:
    return by_label_token.get(_normalize_report_token(str(label or "")))


class MissionWorkstreamProjection(BaseModel):
    """A planned workstream and whether its Tasks finished, partly, or blocked."""

    name: str
    #: ``completed`` | ``partial`` | ``blocked`` — never a raw Task status.
    status: str
    reason: str | None = None
    task_count: int = 0
    completed_task_count: int = 0
    task_ids: list[TaskId] = Field(default_factory=list)


class MissionTaskProjection(BaseModel):
    """Human-readable Task summary — a projection, never a raw Task dump."""

    task_id: TaskId
    title: str
    status: str
    required: bool
    workstream: str
    blocked_reason: str | None = None


class MissionCriterionProjection(BaseModel):
    criterion: str
    met: bool
    reason: str | None = None
    evidence_count: int = 0


class MissionProjection(BaseModel):
    """Additive Mission view; absent on reports stored before this existed."""

    mission_id: MissionId
    objective: str
    status: str
    workstreams: list[MissionWorkstreamProjection] = Field(default_factory=list)
    tasks: list[MissionTaskProjection] = Field(default_factory=list)
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    completed_task_count: int = 0
    partial_task_count: int = 0
    blocked_task_count: int = 0
    success_criteria: list[MissionCriterionProjection] = Field(default_factory=list)
    verified_finding_count: int = 0
    evidence_count: int = 0


class RunEffectiveness(BaseModel):
    successful_work_per_action: float = 0.0
    evidence_per_action: float = 0.0
    verified_findings_per_action: float = 0.0
    dead_end_work_ratio: float = 0.0


# Termination reasons that may still release. NO_ACTIONABLE_PLAN is the
# depleted-frontier exit; GOAL_ACHIEVED is accepted only when the central
# contract defines it (getattr keeps this working before it does) and only
# beside the meaningful-success proofs below (mission completed, criteria
# met, required tasks done) -- the reason alone never certifies success.
_RELEASE_OK_REASONS = (
    TerminationReason.NO_ACTIONABLE_PLAN,
    getattr(TerminationReason, "GOAL_ACHIEVED", TerminationReason.NO_ACTIONABLE_PLAN),
)


class TerminalReportProjection(BaseModel):
    run_id: RunId
    phase: AgentPhase
    termination_reason: str | None
    planning_diagnostic: DecisionDiagnostic | None = None
    pending_event_count: int = 0
    event_delivery_pending: bool = False
    pending_approval_id: ApprovalId | None = None
    ready_for_release: bool
    actions_executed: int
    finding_count: int
    evidence_count: int
    work_status_counts: dict[str, int] = Field(default_factory=dict)
    findings: list[FindingProjection] = Field(default_factory=list)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    integrity_blockers: list[str] = Field(default_factory=list)
    unresolved_hypotheses: list[str] = Field(default_factory=list)
    outstanding_cleanup: list[str] = Field(default_factory=list)
    #: Required-scope split of unresolved_hypotheses. Additive only:
    #: unresolved_hypotheses keeps the full list, these two scope it.
    required_unresolved_hypotheses: list[str] = Field(default_factory=list)
    deferred_hypotheses: list[str] = Field(default_factory=list)
    #: Optional so reports persisted before Mission reporting existed still load.
    mission: MissionProjection | None = None
    #: Gap projection beside the mission view; absent on reports stored
    #: before coverage existed, which read as unknown, never complete.
    coverage: CoverageGapSummary | None = None
    coverage_status: str = "unknown"
    result_processing: list[MappingStatus] = Field(default_factory=list)
    effectiveness: RunEffectiveness = Field(default_factory=RunEffectiveness)


class ReportingAgent:
    """Creates no facts; it only projects already canonical records."""

    async def project(
        self,
        runtime: RuntimeState,
        *,
        graph: AttackGraphService,
        evidence_store: EvidenceStore,
        work_store: WorkStore,
        task_store: MissionTaskStore | None = None,
        required_hypothesis_ids: Collection[str] | None = None,
        agent_definitions: Iterable[AgentDefinition] = (),
        coverage: CoverageGapSummary | None = None,
    ) -> TerminalReportProjection:
        if not runtime.is_terminal:
            raise ValueError("reporting projection requires a terminal runtime")

        integrity = await graph.integrity_report()
        cleanup = await graph.cleanup_report()
        unresolved = list(integrity.unresolved_hypotheses)
        if required_hypothesis_ids is None:
            required_unresolved = list(unresolved)
            deferred = []
        else:
            required = {str(item) for item in required_hypothesis_ids}
            required_unresolved = [item for item in unresolved if str(item) in required]
            deferred = [item for item in unresolved if str(item) not in required]
        evidence = [
            item
            for item in await evidence_store.list_for_run(runtime.run_id)
            if item.engagement_id == runtime.engagement_id
        ]
        current_evidence_ids = {str(item.evidence_id) for item in evidence}
        findings = [
            item
            for item in await graph.list_findings()
            if not set(item.evidence_ids).isdisjoint(current_evidence_ids)
        ]
        work = await work_store.list_items(run_id=runtime.run_id)
        status_counts = Counter(item.status.value for item in work)
        projected_findings = [
            FindingProjection(
                finding_id=FindingId(item.finding_id),
                title=redact_text(item.title),
                severity=item.severity,
                verified=item.verified,
                evidence_ids=sorted(set(item.evidence_ids) & current_evidence_ids),
            )
            for item in findings
        ]
        mission = None
        if task_store is not None:
            plan = await _resolve_mission(task_store, runtime)
            if plan is not None:
                tasks = await task_store.list_tasks(mission_id=plan.mission_id)
                mission = _project_mission(
                    plan,
                    tasks,
                    verified_finding_count=sum(1 for item in projected_findings if item.verified),
                    evidence_count=len(evidence),
                    agent_definitions=agent_definitions,
                )
                if coverage is None:
                    coverage = project_coverage_gaps(mission=plan, tasks=tasks)
        actions = runtime.actions_executed
        terminal_work = sum(status_counts.values())
        dead_ends = sum(
            status_counts.get(status, 0) for status in ("failed", "blocked", "stale", "cancelled")
        )
        effectiveness = RunEffectiveness(
            successful_work_per_action=(
                round(status_counts.get("succeeded", 0) / actions, 6) if actions else 0.0
            ),
            evidence_per_action=(round(len(evidence) / actions, 6) if actions else 0.0),
            verified_findings_per_action=(
                round(
                    sum(item.verified for item in projected_findings) / actions,
                    6,
                )
                if actions
                else 0.0
            ),
            dead_end_work_ratio=(round(dead_ends / terminal_work, 6) if terminal_work else 0.0),
        )
        return TerminalReportProjection(
            run_id=runtime.run_id,
            phase=runtime.phase,
            termination_reason=runtime.termination_reason,
            planning_diagnostic=runtime.planning_diagnostic,
            pending_event_count=runtime.pending_event_count,
            event_delivery_pending=runtime.event_delivery_pending,
            pending_approval_id=runtime.pending_approval_id,
            ready_for_release=(
                runtime.phase is AgentPhase.FINISHED
                and runtime.termination_reason in _RELEASE_OK_REASONS
                and not integrity.blocking
                # Required scope only: non-required open hypotheses are
                # reported as deferred and never alone block release.
                and not required_unresolved
                and cleanup.is_clear
                # A depleted candidate frontier is not evidence that the
                # Mission's required work or success criteria were met.
                and (
                    mission is None
                    and runtime.mission_id is None
                    or mission is not None
                    and mission.status == MissionStatus.COMPLETED.value
                    and all(criterion.met for criterion in mission.success_criteria)
                    and not any(
                        task.required and task.status != TaskStatus.COMPLETED.value
                        for task in mission.tasks
                    )
                )
            ),
            actions_executed=runtime.actions_executed,
            finding_count=len(projected_findings),
            evidence_count=len(evidence),
            work_status_counts=dict(sorted(status_counts.items())),
            findings=projected_findings,
            evidence_ids=[item.evidence_id for item in evidence],
            integrity_blockers=integrity.blocking,
            unresolved_hypotheses=unresolved,
            required_unresolved_hypotheses=required_unresolved,
            deferred_hypotheses=deferred,
            outstanding_cleanup=[str(item.node_id) for item in cleanup.outstanding],
            mission=mission,
            effectiveness=effectiveness,
            coverage=coverage,
            coverage_status=(
                "unknown" if coverage is None or coverage.unknown else "ok"
            ),
            result_processing=[MappingStatus.model_validate(item)
                               for item in project_run_processing(evidence).values()],
        )


async def _resolve_mission(
    task_store: MissionTaskStore, runtime: RuntimeState
) -> MissionPlan | None:
    """Resolve the Mission for a Run without ever guessing between several.

    ``runtime.mission_id`` is the canonical link. Only when it is absent and
    exactly one Mission exists for the Engagement do we fall back to it;
    multiple candidates are ambiguous, so we report no Mission rather than
    attribute one Run's outcome to another's plan.
    """
    if runtime.mission_id is not None:
        # An explicit checkpoint link is authoritative. Never substitute a
        # different Mission merely because it is the only one in the store.
        mission = await task_store.get_mission(runtime.mission_id)
        return (
            mission
            if mission is not None and mission.engagement_id == runtime.engagement_id
            else None
        )
    missions = await task_store.list_missions(engagement_id=runtime.engagement_id)
    if len(missions) == 1:
        return missions[0]
    return None


def _project_mission(
    mission: MissionPlan,
    tasks: list[MissionTask],
    *,
    verified_finding_count: int,
    evidence_count: int,
    agent_definitions: Iterable[AgentDefinition] = (),
) -> MissionProjection:
    """Project Mission/Task records into a readable, secret-free summary."""
    definitions = list(agent_definitions) if agent_definitions else []
    if definitions:
        by_agent_id, by_task_token, by_label_token = _index_agent_definitions(definitions)
        typed_names: dict[str, str] = {}
        for definition in definitions:
            typed_names.setdefault(_definition_group_key(definition), str(definition.name))
    else:
        by_agent_id, by_task_token, by_label_token = {}, {}, {}
        typed_names = {}
    grouped: dict[str | None, list[MissionTask]] = {}
    task_projections: list[MissionTaskProjection] = []
    for task in tasks:
        matched = _match_task_definition(task, by_agent_id, by_task_token) if definitions else None
        if matched is not None:
            key: str | None = _definition_group_key(matched)
            label = str(matched.name)
        else:
            key = _workstream_key(
                " ".join(
                    part
                    for part in (
                        task.required_capability or "",
                        str(task.assigned_agent or ""),
                        task.title,
                        task.objective,
                    )
                    if part
                )
            )
            label = _WORKSTREAM_LABELS.get(key or "", key or "General")
        grouped.setdefault(key, []).append(task)
        task_projections.append(
            MissionTaskProjection(
                task_id=task.task_id,
                title=redact_text(task.title),
                status=task.status.value,
                required=task.required,
                workstream=label,
                blocked_reason=redact_text(task.blocking_reason) if task.blocking_reason else None,
            )
        )

    workstreams: list[MissionWorkstreamProjection] = []
    covered: set[str | None] = set()
    for label in mission.initial_workstreams:
        matched_label = _match_workstream_definition(label, by_label_token) if definitions else None
        if matched_label is not None:
            key = _definition_group_key(matched_label)
            display = str(matched_label.name)
        else:
            key = _workstream_key(label)
            display = label
        if key in covered:
            continue
        covered.add(key)
        workstreams.append(_build_workstream(display, grouped.pop(key, [])))
    for key, group in grouped.items():
        if key in covered:
            continue
        if key in typed_names:
            display = typed_names[key]
        else:
            display = _WORKSTREAM_LABELS.get(key or "", key or "General")
        workstreams.append(_build_workstream(display, group))

    status_counts = Counter(task.status.value for task in tasks)
    completed = status_counts.get(TaskStatus.COMPLETED.value, 0)
    blocked = status_counts.get(TaskStatus.BLOCKED.value, 0) + status_counts.get(
        TaskStatus.FAILED.value, 0
    )
    return MissionProjection(
        mission_id=mission.mission_id,
        objective=redact_text(mission.objective),
        status=mission.status.value,
        workstreams=workstreams,
        tasks=task_projections,
        task_status_counts=dict(sorted(status_counts.items())),
        completed_task_count=completed,
        partial_task_count=len(tasks) - completed - blocked,
        blocked_task_count=blocked,
        success_criteria=_project_criteria(mission),
        verified_finding_count=verified_finding_count,
        evidence_count=evidence_count,
    )


def _build_workstream(name: str, tasks: list[MissionTask]) -> MissionWorkstreamProjection:
    statuses = Counter(task.status.value for task in tasks)
    total = len(tasks)
    task_ids = [task.task_id for task in tasks]
    if total == 0:
        return MissionWorkstreamProjection(
            name=redact_text(name), status="blocked", reason="no task covers this workstream"
        )
    completed = statuses.get(TaskStatus.COMPLETED.value, 0)
    if completed == total:
        return MissionWorkstreamProjection(
            name=redact_text(name),
            status="completed",
            task_count=total,
            completed_task_count=completed,
            task_ids=task_ids,
        )
    stalled = [task for task in tasks if task.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)]
    if completed == 0 and stalled:
        reasons = [
            task.blocking_reason
            if task.status is TaskStatus.BLOCKED and task.blocking_reason
            else f"{task.title} {task.status.value}"
            for task in stalled
        ]
        return MissionWorkstreamProjection(
            name=redact_text(name),
            status="blocked",
            reason=redact_text("; ".join(sorted(set(reasons)))),
            task_count=total,
            completed_task_count=completed,
            task_ids=task_ids,
        )
    return MissionWorkstreamProjection(
        name=redact_text(name),
        status="partial",
        reason=f"{completed} of {total} tasks completed",
        task_count=total,
        completed_task_count=completed,
        task_ids=task_ids,
    )


def _project_criteria(mission: MissionPlan) -> list[MissionCriterionProjection]:
    results = {result.criterion: result for result in mission.criterion_results}
    projections = [
        MissionCriterionProjection(
            criterion=redact_text(criterion),
            met=results[criterion].met,
            reason=(
                redact_text(str(results[criterion].reason)) if results[criterion].reason else None
            ),
            evidence_count=len(results[criterion].evidence_ids),
        )
        if criterion in results
        else MissionCriterionProjection(
            criterion=redact_text(criterion), met=False, reason="not yet evaluated"
        )
        for criterion in mission.success_criteria
    ]
    known = set(mission.success_criteria)
    projections.extend(
        MissionCriterionProjection(
            criterion=redact_text(result.criterion),
            met=result.met,
            reason=redact_text(result.reason) if result.reason else None,
            evidence_count=len(result.evidence_ids),
        )
        for result in mission.criterion_results
        if result.criterion not in known
    )
    return projections
