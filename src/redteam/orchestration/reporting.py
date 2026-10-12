"""Read-only terminal projection for the Reporting role."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import completion_conditions, completion_contract_hash
from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.enums import AgentPhase, MissionStatus, TaskStatus
from redteam.core.ids import ApprovalId, EvidenceId, FindingId, MissionId, RunId, TaskId
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.task_graph import InvestigationReader
from redteam.orchestration.context_projection import MappingStatus, project_run_processing
from redteam.orchestration.coverage import CoverageGapSummary, project_coverage_gaps
from redteam.orchestration.diagnostics import DecisionDiagnostic, safe_feedback
from redteam.orchestration.operator_intents.redaction import redact_text
from redteam.orchestration.runtime_observation import MAX_RUNTIME_OBSERVATIONS, RuntimeObservation
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
    completion_contract_version: int = 1
    completion_contract_hash: str | None = None
    completion_criteria: list[dict[str, Any]] = Field(default_factory=list)


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


# F1 report connection. All additive: existing fields/order never change.
# Detail canon lives in the existing evidence store
# (metadata record_kind == investigation_unresolved); runtime keeps refs only.
# Graph-handler branches are NOT interpreted here (owner B/root only): we read
# exactly record_kind == investigation_unresolved. No new store, no raw text.
_UNRESOLVED_DETAIL_LIMIT = 20
_UNRESOLVED_REF_LIMIT = 50
_READ_PROJECTION_LIMIT = 10


def _safe_nonneg_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _safe_id_token(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()[:limit]
    return text or None


def _safe_id_list(values: Any, limit: int = 20) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    cleaned: list[str] = []
    for item in list(values)[:limit]:
        token = _safe_id_token(item)
        if token is not None:
            cleaned.append(token)
    return cleaned


def _safe_text_list(values: Any, limit: int = 10, chars: int = 500) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    cleaned: list[str] = []
    for item in list(values)[:limit]:
        if item is None:
            continue
        text = safe_feedback(str(item), chars)
        if text:
            cleaned.append(text)
    return cleaned


class EvidenceReadProjection(BaseModel):
    """Durable lookup spec only: offset/query request plus outcome status."""

    evidence_id: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    query: str | None = None
    status: str = "unknown"
    delegation_id: str | None = None


class UnresolvedGapProjection(BaseModel):
    code: str = "unknown"
    description: str = ""
    unresolved_questions: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    required_conditions: list[str] = Field(default_factory=list)
    next_suggestions: list[str] = Field(default_factory=list)


class InvestigationUnresolvedProjection(BaseModel):
    evidence_id: str = ""
    reported_by: str = "unknown"
    mission_id: str | None = None
    task_id: str | None = None
    path_id: str | None = None
    path_version: int | None = None
    path_step_id: str | None = None
    outcome: str = "unknown"
    reason: str = ""
    source_evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[UnresolvedGapProjection] = Field(default_factory=list)
    claim_supported: bool = False
    diagnostic: DecisionDiagnostic | None = None


def _project_evidence_reads(runtime: Any) -> list[EvidenceReadProjection]:
    entries = getattr(runtime, "evidence_reads", None)
    if not isinstance(entries, (list, tuple)):
        return []
    projected: list[EvidenceReadProjection] = []
    for item in list(entries)[-_READ_PROJECTION_LIMIT:]:
        if not isinstance(item, dict):
            continue
        request: Any = item.get("request") if isinstance(item.get("request"), dict) else {}
        status_raw = str(item.get("status", "unknown"))
        status = status_raw.strip()[:60] or "unknown"
        projected.append(
            EvidenceReadProjection(
                evidence_id=str(request.get("evidence_id", ""))[:200],
                offset=_safe_nonneg_int(request.get("offset", 0)),
                limit=_safe_nonneg_int(request.get("limit", 0)),
                query=safe_feedback(str(request["query"]), 200)
                if request.get("query") is not None
                else None,
                status=status,
                delegation_id=_safe_id_token(item.get("delegation_id")),
            )
        )
    return projected


def _project_unresolved_details(evidence: Any) -> list[InvestigationUnresolvedProjection]:
    ordered: list[Any] = []
    for item in evidence or []:
        metadata = getattr(item, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        if metadata.get("record_kind") != "investigation_unresolved":
            continue
        ordered.append(item)
    ordered = ordered[:_UNRESOLVED_DETAIL_LIMIT]
    projected: list[InvestigationUnresolvedProjection] = []
    for item in ordered:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        gaps: list[UnresolvedGapProjection] = []
        raw_gaps = metadata.get("gaps")
        if isinstance(raw_gaps, (list, tuple)):
            for gap in list(raw_gaps)[:10]:
                if not isinstance(gap, dict):
                    continue
                gaps.append(
                    UnresolvedGapProjection(
                        code=safe_feedback(str(gap.get("code", "unknown")), 100) or "unknown",
                        description=safe_feedback(str(gap.get("description", "")), 500),
                        unresolved_questions=_safe_text_list(
                            gap.get("unresolved_questions", gap.get("questions", [])),
                        ),
                        target_node_ids=_safe_id_list(
                            gap.get("target_node_ids", gap.get("targets", []))
                        ),
                        evidence_refs=_safe_id_list(
                            gap.get("evidence_ids", gap.get("evidence_refs", []))
                        ),
                        required_conditions=_safe_text_list(
                            gap.get("required_conditions", gap.get("conditions", []))
                        ),
                        next_suggestions=_safe_text_list(
                            gap.get("next_suggestions", gap.get("suggestions", []))
                        ),
                    )
                )
        reported = safe_feedback(str(metadata.get("reported_by", "unknown")), 60)
        projected.append(
            InvestigationUnresolvedProjection(
                evidence_id=str(getattr(item, "evidence_id", ""))[:200],
                reported_by=reported or "unknown",
                mission_id=_safe_id_token(metadata.get("mission_id")),
                task_id=_safe_id_token(metadata.get("task_id")),
                path_id=_safe_id_token(metadata.get("path_id")),
                path_version=metadata.get("path_version")
                if isinstance(metadata.get("path_version"), int)
                and not isinstance(metadata.get("path_version"), bool)
                else None,
                path_step_id=_safe_id_token(metadata.get("path_step_id")),
                outcome=safe_feedback(str(metadata.get("outcome", "unknown")), 60) or "unknown",
                reason=safe_feedback(str(metadata.get("reason", "")), 500),
                source_evidence_ids=_safe_id_list(metadata.get("source_evidence_ids", [])),
                gaps=gaps,
                claim_supported=False,
                diagnostic=(
                    DecisionDiagnostic.model_validate(metadata["diagnostic"])
                    if isinstance(metadata.get("diagnostic"), dict)
                    else None
                ),
            )
        )
    return projected


def _project_run_outcome(
    *,
    ready_for_release: bool,
    phase: Any,
    has_unresolved_detail: bool,
) -> str:
    if ready_for_release:
        return "goal_achieved"
    if phase not in {AgentPhase.FINISHED, AgentPhase.FAILED}:
        return "in_progress"
    if has_unresolved_detail:
        return "unresolved"
    if phase is AgentPhase.FAILED:
        return "failed"
    if phase is AgentPhase.FINISHED:
        return "unresolved"
    return "unknown"


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
    previous_termination_reason: str | None = None
    planning_diagnostic: DecisionDiagnostic | None = None
    planning_diagnostic_history: list[DecisionDiagnostic] = Field(default_factory=list)
    runtime_observations: list[RuntimeObservation] = Field(
        default_factory=list, max_length=MAX_RUNTIME_OBSERVATIONS,
    )
    request_observations: list[dict[str, Any]] = Field(default_factory=list)
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
    # F1 additive outcome/usage/unresolved projections. Defaults keep reports
    # stored before this existed loadable; they read as unknown/empty, never
    # as success.
    run_outcome: str = "unknown"
    model_requests_used: int = Field(default=0, ge=0)
    model_tokens_used: int = Field(default=0, ge=0)
    model_usage_unknown: int = Field(default=0, ge=0)
    llm_request_accounting: dict[str, Any] | None = None
    unresolved_evidence_ids: list[str] = Field(default_factory=list)
    evidence_reads: list[EvidenceReadProjection] = Field(default_factory=list)
    unresolved_details: list[InvestigationUnresolvedProjection] = Field(default_factory=list)
    evidence_validity: list[dict[str, Any]] = Field(default_factory=list)
    evidence_validity_blockers: list[str] = Field(default_factory=list)
    investigations: list[dict[str, Any]] = Field(default_factory=list)
    executions: list[dict[str, Any]] = Field(default_factory=list)
    criterion_evidence: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_questions: list[dict[str, Any]] = Field(default_factory=list)
    cleanup_obligations: list[dict[str, Any]] = Field(default_factory=list)
    cleanup_unmapped_tools: list[str] = Field(default_factory=list)
    effective_limits: dict[str, Any] = Field(default_factory=dict)
    feature_availability: dict[str, Any] = Field(default_factory=dict)


class ReportingAgent:
    """Creates no facts; it only projects already canonical records."""

    async def project(
        self,
        runtime: RuntimeState,
        *,
        graph: AttackGraphService,
        evidence_store: EvidenceStore,
        work_store: WorkStore | None,
        task_store: InvestigationReader | None = None,
        required_hypothesis_ids: Collection[str] | None = None,
        agent_definitions: Iterable[AgentDefinition] = (),
        coverage: CoverageGapSummary | None = None,
        coordinator: Any | None = None,
        central_state: Any | None = None,
    ) -> TerminalReportProjection:
        if not runtime.is_terminal and central_state is None:
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
        from redteam.orchestration.evidence_validity import project_validity

        validity = await project_validity(
            evidence_store, graph, str(runtime.run_id), records=evidence,
        )
        findings = [
            item
            for item in await graph.list_findings()
            if not set(item.evidence_ids).isdisjoint(current_evidence_ids)
        ]
        if central_state is None and work_store is None:
            raise ValueError("reporting_original_execution_store_missing")
        work: list[Any]
        if central_state is not None:
            work = await central_state.list_executions()
        else:
            assert work_store is not None
            work = list(await work_store.list_items(run_id=runtime.run_id))
        status_counts = Counter((item.state if central_state is not None else item.status).value
                                for item in work)
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
        reuse_support: set[str] = set()
        if task_store is not None:
            plan = await _resolve_mission(task_store, runtime)
            if plan is not None:
                reuse_support.update(str(identity) for result in plan.criterion_results
                                     if result.met for identity in result.evidence_ids)
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
        # Preserve historical Mission/Task labels; only today's release
        # assessment changes. An unused sibling's stale source is not a blocker.
        by_id = {str(item.evidence_id): item for item in evidence}
        pending_support = list(reuse_support)
        while pending_support:
            record = by_id.get(pending_support.pop())
            if record is None:
                continue
            for source in record.metadata.get("source_evidence_ids", []):
                if str(source) not in reuse_support:
                    reuse_support.add(str(source))
                    pending_support.append(str(source))
        validity_blockers = [item.evidence_id for item in validity
                             if item.evidence_id in reuse_support
                             and item.status not in {"unbound", "valid"}]
        actions = (sum(item.result_evidence_id is not None for item in work)
                   if central_state is not None else runtime.actions_executed)
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
        # F1 additive reads: refs stay IDs only; detail canon is looked up from
        # the existing evidence store here, never copied as raw into runtime.
        unresolved_refs = [
            str(item)[:200] for item in (getattr(runtime, "unresolved_evidence_ids", None) or [])
        ][-_UNRESOLVED_REF_LIMIT:]
        unresolved_details = _project_unresolved_details(evidence)
        if unresolved_refs:
            ref_order = {identity: index for index, identity in enumerate(unresolved_refs)}
            unresolved_details = sorted(
                unresolved_details,
                key=lambda item: ref_order.get(item.evidence_id, len(ref_order)),
            )
        evidence_read_projections = _project_evidence_reads(runtime)
        request_accounting = None
        if central_state is not None:
            request_accounting = await central_state.request_ledger().snapshot(str(runtime.run_id))
        elif runtime.llm_request_policy is not None and coordinator is not None:
            from redteam.orchestration.llm_requests import RunRequestLedger

            request_accounting = await RunRequestLedger(coordinator).snapshot(str(runtime.run_id))
        report = TerminalReportProjection(
            run_id=runtime.run_id,
            llm_request_accounting=request_accounting,
            phase=runtime.phase,
            termination_reason=runtime.termination_reason,
            planning_diagnostic=runtime.planning_diagnostic,
            planning_diagnostic_history=list(runtime.planning_diagnostic_history),
            runtime_observations=list(runtime.recent_observations[-MAX_RUNTIME_OBSERVATIONS:]),
            request_observations=[
                {
                    "tool_id": str(item.metadata["tool_result"].get("tool_id", "")),
                    "target": safe_feedback(str(item.metadata.get("target_node_id", ""))),
                    **{
                        key: safe_feedback(str(item.metadata["tool_result"]["output"][key]))
                        for key in ("scheme", "host", "port", "status_code")
                        if key in item.metadata["tool_result"]["output"]
                    },
                }
                for item in evidence
                if isinstance(item.metadata.get("tool_result"), dict)
                and isinstance(item.metadata["tool_result"].get("output"), dict)
                and "scheme" in item.metadata["tool_result"]["output"]
            ][-20:],
            pending_event_count=runtime.pending_event_count,
            event_delivery_pending=runtime.event_delivery_pending,
            pending_approval_id=runtime.pending_approval_id,
            ready_for_release=(
                runtime.phase is AgentPhase.FINISHED
                and runtime.termination_reason in _RELEASE_OK_REASONS
                and not integrity.blocking
                and not validity_blockers
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
            actions_executed=actions,
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
            coverage_status=("unknown" if coverage is None or coverage.unknown else "ok"),
            result_processing=[
                MappingStatus.model_validate(item)
                for item in project_run_processing(evidence).values()
            ],
            model_requests_used=_safe_nonneg_int(getattr(runtime, "model_requests_used", 0)),
            model_tokens_used=_safe_nonneg_int(getattr(runtime, "model_tokens_used", 0)),
            model_usage_unknown=_safe_nonneg_int(getattr(runtime, "model_usage_unknown", 0)),
            unresolved_evidence_ids=unresolved_refs,
            evidence_reads=evidence_read_projections,
            unresolved_details=unresolved_details,
            evidence_validity=[item.model_dump(mode="json") for item in validity],
            evidence_validity_blockers=validity_blockers,
            run_outcome="unknown",
        )
        if central_state is not None:
            from types import SimpleNamespace

            from redteam.orchestration.central_completion import (
                goal_inputs,
                required_questions,
                unsafe,
            )
            assert request_accounting is not None

            tasks = await central_state.list_tasks()
            plan = await central_state.get_mission(runtime.mission_id)
            report.investigations = [task.model_dump(mode="json") for task in tasks]
            report.executions = [item.model_dump(mode="json") for item in work]
            report.criterion_evidence = [item.model_dump(mode="json")
                                         for item in plan.criterion_results]
            required_open = {(task_id, q.question_id) for task_id, q in required_questions(tasks)}
            report.unresolved_questions = [
                {"task_id": str(task.task_id), **q.model_dump(mode="json"),
                 "resolved": q.required_for_goal and
                 (str(task.task_id), q.question_id) not in required_open}
                for task in tasks if task.result for q in task.result.questions
            ]
            report.cleanup_obligations = [item.model_dump(mode="json") for item in cleanup.items]
            from redteam.orchestration.resource_cleanup import unmapped_resource_effects
            report.cleanup_unmapped_tools = await unmapped_resource_effects(evidence_store, work)
            report.model_requests_used = request_accounting["spent"]
            report.model_tokens_used = request_accounting["known_tokens"]
            report.model_usage_unknown = request_accounting["unknown_usage"]
            report.ready_for_release = report.ready_for_release and (
                not required_open and not any(unsafe(item) for item in work)
                and bool(plan.success_criteria)
                and {row.criterion for row in plan.criterion_results} == set(plan.success_criteria)
            )
            from redteam.orchestration.result_claims import mission_result_purpose
            report.ready_for_release = report.ready_for_release and all(
                row.goal_revision == plan.revision - 1
                and row.purpose_hash == mission_result_purpose(plan)
                for row in plan.criterion_results
            )
            if report.ready_for_release:
                # Current originals may disappear/change after settlement.
                # Read-only reports never inherit an old success flag blindly.
                reader = SimpleNamespace(
                    central_state=central_state,
                    evidence_store=evidence_store, investigations=central_state, path_store=None,
                )
                try:
                    proof_errors = (await goal_inputs(reader, runtime, graph_service=graph))[3]
                except (ValueError, RuntimeError) as exc:
                    proof_errors = [type(exc).__name__]
                report.integrity_blockers.extend(proof_errors)
                report.ready_for_release = not proof_errors
        report.run_outcome = _project_run_outcome(
            ready_for_release=report.ready_for_release,
            phase=report.phase,
            has_unresolved_detail=bool(unresolved_details or unresolved_refs),
        )
        return report


async def _resolve_mission(
    task_store: InvestigationReader, runtime: RuntimeState
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
                completion_contract_version=task.completion_contract_version,
                completion_contract_hash=completion_contract_hash(task),
                completion_criteria=completion_conditions(task),
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
