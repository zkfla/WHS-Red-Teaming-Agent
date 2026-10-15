"""Validated strategy/atomization -> native MissionTask, never a second queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.core.contracts.completion import (
    CompletionConditionConflict,
    CompletionCriterion,
    completion_condition_conflicts,
    criterion_id,
)
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.graph import HypothesisUpdate
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.contracts.specialists import ProbeLimits
from redteam.core.enums import TaskStatus
from redteam.core.ids import AgentId, EventId, EvidenceId, GraphNodeId, HypothesisId, RunId, TaskId
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.coordination import content_hash

if TYPE_CHECKING:
    from redteam.orchestration.central_state import CheckedEvidence


class InvestigationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    target_node_ids: list[str] = Field(min_length=1)
    principal_refs: list[str] = Field(default_factory=list)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1)


class AtomicTaskProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_key: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    title: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    target_node_ids: list[str] = Field(min_length=1)
    principal_refs: list[str] = Field(default_factory=list)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    source_result_id: str | None = None
    source_question_id: str | None = None


class StrategyTasks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[InvestigationQuestion] = Field(min_length=1)
    tasks: list[AtomicTaskProposal] = Field(min_length=1)


class SpecialistProposalError(ValueError):
    """Code-owned validation label; never contains model text or secrets."""

    def __init__(
        self, code: str, *,
        condition_conflicts: tuple[CompletionConditionConflict, ...] = (),
    ) -> None:
        self.code = code
        self.condition_conflicts = condition_conflicts
        if condition_conflicts:
            from redteam.orchestration.diagnostics import DecisionDiagnostic

            self.diagnostic = DecisionDiagnostic(
                code=code,
                stage="hypothesis_planning",
                kind="validation",
                summary=(
                    "One investigation purpose contains both confirmation and refutation "
                    "conditions for the same hypothesis."
                ),
                retryable=True,
                cause_scope="model_input",
                owner="planner",
                repair_condition=(
                    "Keep only one candidate verdict condition in each AND condition set; "
                    "preserve observations and conditions for other candidates."
                ),
                criterion_ids=sorted({
                    criterion_id for conflict in condition_conflicts
                    for criterion_id in conflict.criterion_ids
                }),
                trace=[{
                    "kind": "same_candidate_completion_condition_conflict",
                    "hypothesis_id": conflict.hypothesis_id,
                    "criterion_ids": list(conflict.criterion_ids),
                } for conflict in condition_conflicts],
            )
        super().__init__(code)




def validate_strategy_questions(
    questions: list[InvestigationQuestion], *, known_targets: set[str],
    known_principals: set[str],
    known_hypotheses: frozenset[str] = frozenset(),
    investigation_only: bool = False,
) -> None:
    if len({q.question_id for q in questions}) != len(questions):
        raise SpecialistProposalError("specialist_duplicate_identity")
    _validate_subjects(questions, known_targets, known_principals, known_hypotheses)


def _validate_subjects(
    items: list[InvestigationQuestion] | list[AtomicTaskProposal] | list[ProbeTaskProposal],
    known_targets: set[str], known_principals: set[str], known_hypotheses: frozenset[str],
) -> None:
    for item in items:
        if not item.objective.strip() or any(not ref.strip() for ref in item.target_node_ids):
            raise SpecialistProposalError("specialist_blank_subject")
        if isinstance(item, AtomicTaskProposal) and not item.title.strip():
            raise SpecialistProposalError("specialist_blank_title")
        if set(item.target_node_ids) - known_targets:
            raise SpecialistProposalError("specialist_unknown_target")
        if set(item.principal_refs) - known_principals:
            raise SpecialistProposalError("specialist_unknown_principal")
        identities = [criterion_id(c) for c in item.completion_criteria]
        if len(identities) != len(set(identities)):
            raise SpecialistProposalError("specialist_duplicate_completion_condition")
        for condition in item.completion_criteria:
            if not condition.is_known() or not condition.description.strip():
                raise SpecialistProposalError("specialist_unknown_completion_condition")
            if set(condition.principal_refs) - set(item.principal_refs):
                raise SpecialistProposalError("specialist_condition_principal_outside_subjects")
            if condition.hypothesis_id and condition.hypothesis_id not in known_hypotheses:
                raise SpecialistProposalError("specialist_unknown_hypothesis")
        conflicts = completion_condition_conflicts(item.completion_criteria)
        if conflicts:
            raise SpecialistProposalError(
                "same_candidate_completion_condition_conflict",
                condition_conflicts=conflicts,
            )


def validate_strategy_tasks(
    proposal: StrategyTasks, *, agents: AgentRegistry, known_targets: set[str],
    known_principals: set[str], max_tasks: int,
    known_hypotheses: frozenset[str] = frozenset(),
    investigation_only: bool = False,
) -> list[str]:
    """Retain question coverage, subjects and exact completion conditions."""
    if max_tasks < 1 or len(proposal.tasks) > max_tasks:
        raise SpecialistProposalError("specialist_task_count_limit")
    validate_strategy_questions(
        proposal.questions, known_targets=known_targets, known_principals=known_principals,
        known_hypotheses=known_hypotheses,
        investigation_only=investigation_only,
    )
    ordered = [t.task_key for t in proposal.tasks]
    if len(set(ordered)) != len(ordered):
        raise SpecialistProposalError("specialist_duplicate_identity")
    questions = {q.question_id: q for q in proposal.questions}
    if {t.question_id for t in proposal.tasks} != set(questions):
        raise SpecialistProposalError("specialist_question_coverage_mismatch")
    _validate_subjects(proposal.tasks, known_targets, known_principals, known_hypotheses)
    grouped = {q: [t for t in proposal.tasks if t.question_id == q] for q in questions}
    for key, question in questions.items():
        children = grouped[key]
        if {r for t in children for r in t.target_node_ids} != set(question.target_node_ids):
            raise SpecialistProposalError("specialist_question_targets_changed")
        if {r for t in children for r in t.principal_refs} != set(question.principal_refs):
            raise SpecialistProposalError("specialist_question_principals_changed")
        original = {criterion_id(c) for c in question.completion_criteria}
        retained = {criterion_id(c) for t in children for c in t.completion_criteria}
        if original - retained:
            raise SpecialistProposalError("specialist_question_completion_conditions_dropped")
        for child in children:
            role_id = AgentId(child.role_id)
            definition = agents.definition(role_id)
            if definition is None or not definition.schedulable or agents.get(role_id) is None:
                raise SpecialistProposalError("specialist_role_not_executable")
    return ordered




async def register_investigation_tasks(
    state, *, proposal_id: str, proposal: StrategyTasks, agents: AgentRegistry,
    known_targets: set[str], known_principals: set[str], max_tasks: int,
    evidence_store, expected_revision: int | None = None,
    known_hypotheses: frozenset[str] = frozenset(),
) -> list[str]:
    """Exact registration identity, without semantic deduplication or a Work queue."""
    from redteam.orchestration.central_state import check_evidence
    from redteam.orchestration.coordination import StateConflict

    keys = validate_strategy_tasks(
        proposal, agents=agents, known_targets=known_targets,
        known_principals=known_principals, max_tasks=max_tasks,
        known_hypotheses=known_hypotheses, investigation_only=True,
    )
    proposal = proposal.model_copy(deep=True)
    checked = await check_evidence(
        evidence_store, run_id=state.run_id, engagement_id=state.engagement_id,
        evidence_ids=[ref for task in proposal.tasks for ref in task.evidence_ids],
    )
    payload_hash = content_hash(proposal)

    def register(tx):
        mission = tx.get_mission(tx.binding()[0])
        if expected_revision is not None and mission.revision != expected_revision:
            raise StateConflict("planning_goal_changed")
        proposed = {item.task_key: item for item in proposal.tasks}
        tasks = []
        for key in keys:
            item = proposed[key]
            identity = TaskId("task_" + content_hash([state.run_id, proposal_id, key]))
            task = MissionTask(
                task_id=identity, mission_id=mission.mission_id, title=item.title,
                objective=item.objective, assigned_agent=AgentId(item.role_id),
                status=TaskStatus.READY, completion_contract_version=2,
                completion_criteria=item.completion_criteria, principal_refs=item.principal_refs,
                evidence_ids=item.evidence_ids,
                related_attack_graph_node_ids=[GraphNodeId(ref) for ref in item.target_node_ids],
                related_hypothesis_ids=[HypothesisId(c.hypothesis_id)
                                        for c in item.completion_criteria if c.hypothesis_id],
                source_task_id=item.source_task_id, source_result_id=item.source_result_id,
                source_question_id=item.source_question_id,
                # Stable across replay; registration time is not model meaning.
                created_at=mission.created_at, updated_at=mission.created_at,
                provenance=Provenance(source="specialist:atomization", metadata={
                    "proposal_id": proposal_id, "proposal_hash": payload_hash,
                    "question_id": item.question_id,
                }),
            )
            tasks.append(task)
        saved = tx.register_batch(proposal_id, tasks, checked=checked)
        for task in saved:
            tx.queue(EventEnvelope(
                event_id=EventId("evt_" + content_hash([str(task.task_id), "created"])),
                event_type=EventType.TASK_CREATED, run_id=RunId(state.run_id),
                engagement_id=state.engagement_id, timestamp=mission.created_at,
                source="orchestration.specialist_tasks", correlation_id=state.run_id,
                payload={"mission_id": str(mission.mission_id), "task_id": str(task.task_id)},
            ))
        return [str(task.task_id) for task in saved]
    return await state.transaction(register)


class TaskNecessityProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1)
    action: Literal["cancel", "set_required"] = "cancel"
    required: bool | None = None
    reason: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    criterion_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    source_result_id: str | None = None
    source_question_id: str | None = None
    owner: str | None = None
    version: int | None = Field(default=None, ge=0)


def validate_necessity_proposals(proposals, *, mission, tasks_by_id, evidence_ids=None) -> None:
    seen: set[str] = set()
    for item in proposals:
        if item.task_id in seen:
            raise SpecialistProposalError("planner_adjustment_duplicate_task")
        seen.add(item.task_id)
        task = tasks_by_id.get(item.task_id)
        if task is None or str(task.mission_id) != str(mission.mission_id):
            raise SpecialistProposalError("planner_adjustment_unknown_task")
        if item.goal_revision != mission.revision:
            raise SpecialistProposalError("planner_adjustment_stale_goal")
        if not item.reason.strip():
            raise SpecialistProposalError("planner_adjustment_blank_reason")
        grounded = False
        if item.criterion_refs:
            if set(item.criterion_refs) - set(mission.success_criteria):
                raise SpecialistProposalError("planner_adjustment_unknown_criterion")
            grounded = True
        if item.evidence_ids:
            if any(not ref.strip() for ref in item.evidence_ids):
                raise SpecialistProposalError("planner_adjustment_blank_evidence_ref")
            if evidence_ids is not None:
                unknown = set(item.evidence_ids) - {str(known) for known in evidence_ids}
                if unknown:
                    raise SpecialistProposalError("planner_adjustment_unknown_evidence")
            grounded = True
        if item.source_task_id is not None:
            source = tasks_by_id.get(item.source_task_id)
            questions = set()
            if source is not None and source.result is not None:
                questions = {q.question_id for q in source.result.questions}
            if (source is None or source.result is None
                    or source.result.result_id != item.source_result_id
                    or item.source_question_id not in questions):
                raise SpecialistProposalError("planner_adjustment_bad_result_link")
            grounded = True
        if not grounded:
            raise SpecialistProposalError("planner_adjustment_ungrounded")
        if item.action == "set_required" and item.required is None:
            raise SpecialistProposalError("planner_adjustment_missing_required_flag")
        if item.action == "cancel" and item.required is True:
            raise SpecialistProposalError("planner_adjustment_cancel_keeps_required")
        owned = task.occupant_owner is not None
        if owned and (item.owner != task.occupant_owner or item.version != task.ownership_version):
            raise SpecialistProposalError("planner_adjustment_stale_owner")
        if not owned and (item.owner is not None or item.version is not None):
            raise SpecialistProposalError("planner_adjustment_unowned_has_owner")


async def apply_necessity_adjustments(
    state, proposals, *, expected_revision=None, evidence_store=None,
) -> list[str]:
    if not proposals:
        return []
    from redteam.orchestration.central_state import check_evidence
    from redteam.orchestration.coordination import CoordinationError, StateConflict

    refs = sorted({ref for item in proposals for ref in item.evidence_ids if ref.strip()})
    checked: tuple[CheckedEvidence, ...] = ()
    if refs:
        if evidence_store is None:
            raise CoordinationError("planner_adjustment_evidence_unverifiable")
        checked = await check_evidence(
            evidence_store, run_id=state.run_id, engagement_id=state.engagement_id,
            evidence_ids=refs,
        )

    def adjust(tx):
        mission = tx.get_mission(tx.binding()[0])
        if expected_revision is not None and mission.revision != expected_revision:
            raise StateConflict("planning_goal_changed")
        if checked:
            tx.add_evidence_refs(checked)
        applied = []
        for item in proposals:
            updated = tx.adjust_task_necessity(
                item.task_id, action=item.action, required=item.required, reason=item.reason,
                goal_revision=item.goal_revision, owner=item.owner, version=item.version,
                criterion_refs=item.criterion_refs, evidence_ids=item.evidence_ids,
                source_task_id=item.source_task_id,
                source_result_id=item.source_result_id,
                source_question_id=item.source_question_id,
            )
            applied.append(str(updated.task_id))
        return applied
    return await state.transaction(adjust)


class ProbeTaskProposal(BaseModel):
    """A bounded purpose proposal, not a second Task or mandatory question entity."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    task_key: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    title: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    target_node_ids: list[str] = Field(min_length=1)
    principal_refs: list[str] = Field(default_factory=list)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_revisions: dict[str, int] = Field(default_factory=dict)
    required: bool = False
    required_reason: str | None = None
    goal_required_hypothesis_ids: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    source_result_id: str | None = None
    source_question_id: str | None = None
    probe_limits: ProbeLimits | None = None

    @model_validator(mode="after")
    def _purpose(self):
        if not all(value.strip() for value in (self.task_key, self.objective, self.title,
                                               self.role_id, *self.target_node_ids)):
            raise ValueError("probe purpose and references must be nonblank")
        if self.required and not (self.required_reason or "").strip():
            raise ValueError("goal-required probe needs an explicit reason")
        return self


class HypothesisProbeProposal(BaseModel):
    """One planner judgment; may only update candidates or register discovery probes."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    updates: list[HypothesisUpdate] = Field(default_factory=list)
    tasks: list[ProbeTaskProposal] = Field(default_factory=list)

    @model_validator(mode="after")
    def _identities(self):
        ids = [item.hypothesis.hypothesis_id for item in self.updates]
        keys = [item.task_key for item in self.tasks]
        if not (ids or keys) or len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise ValueError("empty or repeated candidate/probe proposal identity")
        return self


class _CandidateReviewView:
    """Existing proof checker sees the proposed verdict without first committing it."""

    def __init__(self, service, candidate):
        self.service, self.candidate = service, candidate

    async def get_hypothesis(self, identity):
        if identity == self.candidate.hypothesis_id:
            return self.candidate
        return await self.service.get_hypothesis(identity)

    def __getattr__(self, name):
        return getattr(self.service, name)


def validate_probe_subjects(proposal, *, known_targets, known_principals, known_hypotheses):
    """Reuse criterion/subject rules without manufacturing question entities."""
    _validate_subjects(proposal.tasks, known_targets, known_principals,
                       frozenset(known_hypotheses))


def _candidate_verdict_trace(candidate_id: object, review_id: object) -> list[dict]:
    """Safe candidate and review identity for a rejected verdict; unsafe values are dropped."""
    from redteam.orchestration.coordination import safe_identifier

    row = {}
    for key, value in (("candidate_id", candidate_id), ("review_evidence_id", review_id)):
        text = safe_identifier(value)
        if text is not None:
            row[key] = text
    return [row] if row else []


async def register_probe_tasks(
    deps, *, proposal_id: str, proposal: HypothesisProbeProposal, expected_revision: int,
    expected_binding_hash: str, graph_read: dict[str, str], known_targets: set[str],
    known_principals: set[str], max_tasks: int,
    task_read: dict[str, str] | None = None,
    planner_judgment: dict | None = None,
) -> dict:
    """Validated v2 admission; defaults still use the existing planner until plan5c/d.

    graph_read is the caller's snapshot of every consumed graph subject. Replays
    read the original receipt before CAS; unrelated sibling nodes are not gated.
    """
    from redteam.attack_graph.identity import hypothesis_node_id
    from redteam.attack_graph.service import AttackGraphService
    from redteam.orchestration.central_state import check_evidence
    from redteam.orchestration.completion import validate_result_claims
    from redteam.orchestration.coordination import (
        CandidateAdmissionConflict,
        StateConflict,
        safe_identifiers,
    )
    from redteam.orchestration.diagnostics import DecisionDiagnostic
    from redteam.orchestration.evidence_validity import capture_validity

    proposal = HypothesisProbeProposal.model_validate(proposal.model_dump())
    state = deps.central_state
    if not proposal_id.strip() or max_tasks < 1 or len(proposal.tasks) > max_tasks:
        raise StateConflict("probe_registration_identity_or_count_invalid")
    request_hash = content_hash(proposal)
    previous = await state.transaction(
        lambda tx: tx.probe_registration(proposal_id, request_hash), write=False,
    )
    if previous is not None:
        return previous
    known_hypotheses = {
        item.hypothesis.hypothesis_id for item in proposal.updates
    } | {identity for task in proposal.tasks for identity in task.hypothesis_revisions}
    validate_probe_subjects(proposal, known_targets=known_targets,
                            known_principals=known_principals, known_hypotheses=known_hypotheses)
    mission_id = await state.transaction(lambda tx: tx.binding()[0], write=False)
    mission = await state.get_mission(mission_id)
    if mission is None:
        raise StateConflict("planning_mission_missing")
    refs = {ref for task in proposal.tasks for ref in task.evidence_ids}
    subjects = {ref for task in proposal.tasks for ref in task.target_node_ids}
    for update in proposal.updates:
        candidate = update.hypothesis
        subjects.update(candidate.target_node_ids)
        subjects.update(str(hypothesis_node_id(ref)) for ref in candidate.source_hypothesis_ids)
        if update.expected_revision is not None:
            subjects.add(str(hypothesis_node_id(candidate.hypothesis_id)))
        if set(candidate.target_node_ids) - known_targets or (
            set(candidate.principal_refs) - known_principals
        ):
            raise StateConflict("candidate_subject_not_registered")
        refs.update(candidate.supporting_evidence_ids)
        refs.update(candidate.refuting_evidence_ids)
        if update.review_evidence_id:
            refs.add(update.review_evidence_id)
    for item in proposal.tasks:
        definition = deps.agents.definition(AgentId(item.role_id))
        if definition is None or not definition.schedulable or deps.agents.get(
            AgentId(item.role_id)
        ) is None:
            raise StateConflict("probe_role_not_executable")
        if set(item.target_node_ids) - known_targets or set(item.principal_refs) - known_principals:
            raise StateConflict("probe_subject_not_registered")
        subjects.update(str(hypothesis_node_id(ref)) for ref in item.hypothesis_revisions
                        if ref not in {u.hypothesis.hypothesis_id for u in proposal.updates})
    if subjects - graph_read.keys():
        raise StateConflict("probe_related_read_set_incomplete")
    source_tasks = {task.source_task_id for task in proposal.tasks if task.source_task_id}
    if source_tasks - (task_read or {}).keys():
        raise StateConflict("probe_source_read_set_incomplete")
    checked = await check_evidence(
        deps.evidence_store, run_id=state.run_id, engagement_id=state.engagement_id,
        evidence_ids=sorted(refs),
    )
    service = AttackGraphService(deps.graph_store)
    validity_read = None
    verdicts = [u for u in proposal.updates if u.hypothesis.status in {"confirmed", "rejected"}]
    if verdicts:
        validity_read, errors = await capture_validity(deps, sorted(refs), graph_service=service)
        if errors:
            raise CandidateAdmissionConflict("candidate_evidence_not_current", [DecisionDiagnostic(
                code="candidate_evidence_not_current", stage="candidate_admission",
                kind="validation", location="$.updates",
                summary="Cited evidence validity is not current for the candidate verdict.",
                retryable=False, cause_scope="evidence_validity",
                owner="orchestration.specialist_tasks",
                repair_condition=(
                    "Reassess current evidence validity from the original records and replan "
                    "the candidate verdict; rereading unchanged records does not repair this."
                ),
                evidence_ids=sorted(refs)[:10],
            )])
    for update in verdicts:
        candidate = update.hypothesis
        kind = "hypothesis_confirmed" if candidate.status == "confirmed" else "hypothesis_refuted"
        criterion = CompletionCriterion(kind=kind, description=candidate.description,
                                        hypothesis_id=candidate.hypothesis_id)
        errors = await validate_result_claims(
            criteria=[criterion],
            evidence_ids=[*candidate.supporting_evidence_ids, *candidate.refuting_evidence_ids,
                          *([update.review_evidence_id] if update.review_evidence_id else [])],
            target_node_ids=candidate.target_node_ids, principal_refs=candidate.principal_refs,
            evidence_store=deps.evidence_store,
            graph_service=_CandidateReviewView(service, candidate), run_id=state.run_id,
            path_store=getattr(deps, "path_store", None), mission_id=str(mission.mission_id),
            task_graph_store=state, work_store=getattr(deps, "work_store", None),
        )
        if errors:
            # A stale or wrong review often leaves its own diagnostic without refs. The
            # cited review and the candidate identity are the safe handles for repair.
            verdict_trace = _candidate_verdict_trace(candidate.hypothesis_id,
                                                     update.review_evidence_id)
            diagnostics = [
                item.model_copy(update={
                    "criterion_ids": safe_identifiers(
                        [*item.criterion_ids, criterion_id(criterion)]
                    ),
                    "evidence_ids": safe_identifiers([
                        update.review_evidence_id or "", *item.evidence_ids,
                        *candidate.supporting_evidence_ids, *candidate.refuting_evidence_ids,
                    ]),
                    "trace": [*item.trace, *verdict_trace][-20:],
                })
                for item in list(getattr(errors, "diagnostics", None) or [])
            ]
            if diagnostics:
                raise CandidateAdmissionConflict("candidate_verdict_unproven", diagnostics)
            raise StateConflict("candidate_verdict_unproven")
    policy_limits = getattr(getattr(deps, "specialist_policy", None), "probe_limits", None)
    for item in proposal.tasks:
        if policy_limits is not None and item.probe_limits is not None and (
            item.probe_limits.max_actions > policy_limits.max_actions
            or item.probe_limits.max_model_requests > policy_limits.max_model_requests
        ):
            raise StateConflict("probe_override_exceeds_profile_limits")
    tasks = [MissionTask(
        task_id=TaskId("task_" + content_hash([state.run_id, proposal_id, item.task_key])),
        mission_id=mission.mission_id, title=item.title, objective=item.objective,
        assigned_agent=AgentId(item.role_id), status=TaskStatus.READY,
        investigation_contract_version=2, completion_contract_version=2,
        probe_limits=item.probe_limits or policy_limits,
        completion_criteria=item.completion_criteria, principal_refs=item.principal_refs,
        required=item.required,
        goal_required_hypothesis_ids=[HypothesisId(ref)
                                      for ref in item.goal_required_hypothesis_ids],
        related_attack_graph_node_ids=[GraphNodeId(ref) for ref in item.target_node_ids],
        related_hypothesis_ids=[HypothesisId(ref) for ref in item.hypothesis_revisions],
        hypothesis_revisions=item.hypothesis_revisions,
        evidence_ids=[EvidenceId(ref) for ref in item.evidence_ids],
        source_task_id=TaskId(item.source_task_id) if item.source_task_id else None,
        source_result_id=item.source_result_id,
        source_question_id=item.source_question_id,
        created_at=mission.created_at, updated_at=mission.created_at,
        provenance=Provenance(source="planner:probe", metadata={
            "proposal_id": proposal_id, "required_reason": item.required_reason,
        }),
    ) for item in proposal.tasks]
    notifications = [EventEnvelope(
        event_id=EventId("evt_" + content_hash([proposal_id, str(task.task_id), "created"])),
        event_type=EventType.TASK_CREATED, run_id=RunId(state.run_id),
        engagement_id=state.engagement_id, timestamp=mission.created_at,
        source="orchestration.specialist_tasks", correlation_id=state.run_id,
        payload={"task_id": str(task.task_id), "proposal_id": proposal_id,
                 "producer": "planner", "investigation_contract_version": 2},
    ) for task in tasks]
    if proposal.updates:
        notifications.append(EventEnvelope(
            event_id=EventId("evt_" + content_hash([proposal_id, "candidate_updates"])),
            event_type="HypothesisJudgmentCommitted", run_id=RunId(state.run_id),
            engagement_id=state.engagement_id, timestamp=mission.created_at,
            source="orchestration.specialist_tasks", correlation_id=state.run_id,
            payload={"proposal_id": proposal_id, "producer": "planner",
                     "hypothesis_ids": [u.hypothesis.hypothesis_id for u in proposal.updates]},
        ))
    return await state.transaction(lambda tx: tx.register_hypothesis_probes(
        proposal_id, request_hash, updates=proposal.updates, tasks=tasks, checked=checked,
        expected_revision=expected_revision, expected_binding_hash=expected_binding_hash,
        graph_read=graph_read, notifications=notifications, validity_read=validity_read,
        task_read=task_read,
        planner_judgment=planner_judgment,
    ))
