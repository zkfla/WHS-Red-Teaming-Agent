"""Validated strategy/atomization -> native MissionTask, never a second queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.enums import TaskStatus
from redteam.core.ids import AgentId, EventId, GraphNodeId, HypothesisId, RunId, TaskId
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

    def __init__(self, code: str) -> None:
        self.code = code
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
    items: list[InvestigationQuestion] | list[AtomicTaskProposal],
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
