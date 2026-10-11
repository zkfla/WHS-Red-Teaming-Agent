"""Validated strategy/atomization -> native MissionTask, never a second queue."""

from __future__ import annotations

from datetime import datetime

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.enums import TaskStatus
from redteam.core.ids import AgentId, EventId, GraphNodeId, HypothesisId, MissionId, RunId, TaskId
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.coordination import ChangeSet, content_hash
from redteam.orchestration.planning_admission import PlanningAdmission, PlanningInputStamp


class InvestigationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    target_node_ids: list[str] = Field(min_length=1)
    principal_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
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
    depends_on: list[str] = Field(default_factory=list)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1)


class StrategyTasks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[InvestigationQuestion] = Field(min_length=1)
    tasks: list[AtomicTaskProposal] = Field(min_length=1)


class SpecialistProposalError(ValueError):
    """Code-owned validation label; never contains model text or secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _dag(identities: list[str], edges: list[tuple[str, str]]) -> list[str]:
    if len(set(identities)) != len(identities):
        raise SpecialistProposalError("specialist_duplicate_identity")
    if any(parent not in identities or child not in identities for parent, child in edges):
        raise SpecialistProposalError("specialist_unknown_dependency")
    graph = nx.DiGraph()
    graph.add_nodes_from(identities)
    graph.add_edges_from(edges)
    if not nx.is_directed_acyclic_graph(graph):
        raise SpecialistProposalError("specialist_dependency_cycle")
    return list(nx.topological_sort(graph))


def validate_strategy_questions(
    questions: list[InvestigationQuestion], *, known_targets: set[str],
    known_principals: set[str],
    known_hypotheses: frozenset[str] = frozenset(),
) -> None:
    _dag([q.question_id for q in questions], [
        (parent, q.question_id) for q in questions for parent in q.depends_on
    ])
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
) -> list[str]:
    """Retain question coverage, subjects, exact conditions and dependencies."""
    if max_tasks < 1 or len(proposal.tasks) > max_tasks:
        raise SpecialistProposalError("specialist_task_count_limit")
    validate_strategy_questions(
        proposal.questions, known_targets=known_targets, known_principals=known_principals,
        known_hypotheses=known_hypotheses,
    )
    ordered = _dag([t.task_key for t in proposal.tasks], [
        (parent, t.task_key) for t in proposal.tasks for parent in t.depends_on
    ])
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
        predecessors = {t.task_key for parent in question.depends_on for t in grouped[parent]}
        for child in children:
            role_id = AgentId(child.role_id)
            definition = agents.definition(role_id)
            if definition is None or not definition.schedulable or agents.get(role_id) is None:
                raise SpecialistProposalError("specialist_role_not_executable")
            if predecessors - set(child.depends_on):
                raise SpecialistProposalError("specialist_question_dependencies_dropped")
    return ordered


async def register_strategy_tasks(
    admission: PlanningAdmission, stamp: PlanningInputStamp, *, proposal_id: str,
    proposal: StrategyTasks, agents: AgentRegistry, known_targets: set[str],
    known_principals: set[str], max_tasks: int, now: datetime,
    known_hypotheses: frozenset[str] = frozenset(),
) -> list[str]:
    ordered = validate_strategy_tasks(
        proposal, agents=agents, known_targets=known_targets,
        known_principals=known_principals, max_tasks=max_tasks,
        known_hypotheses=known_hypotheses,
    )
    # Immutable copies survive awaits; the producer cannot mutate an accepted
    # proposal while its atomic callback is waiting to acquire the coordinator.
    payload = proposal.model_dump(mode="json")
    proposed = {t.task_key: t.model_copy(deep=True) for t in proposal.tasks}
    identities = {
        key: TaskId("task_" + content_hash([stamp.run_id, proposal_id, key])) for key in ordered
    }

    async def apply(tx: ChangeSet) -> dict:
        binding = tx.binding(stamp.run_id, stamp.mission_id)
        for key in ordered:
            item = proposed[key]
            task = MissionTask(
                task_id=identities[key], mission_id=MissionId(stamp.mission_id), title=item.title,
                objective=item.objective, assigned_agent=AgentId(item.role_id),
                status=TaskStatus.PENDING if item.depends_on else TaskStatus.READY,
                dependency_task_ids=[identities[parent] for parent in item.depends_on],
                related_attack_graph_node_ids=[GraphNodeId(ref) for ref in item.target_node_ids],
                related_hypothesis_ids=[HypothesisId(c.hypothesis_id)
                                        for c in item.completion_criteria if c.hypothesis_id],
                principal_refs=item.principal_refs, completion_contract_version=2,
                completion_criteria=item.completion_criteria,
                created_at=now, updated_at=now,
                provenance=Provenance(source="specialist:atomization", metadata={
                    "proposal_id": proposal_id, "question_id": item.question_id,
                    "strategy_revision": stamp.strategy_revision + 1,
                }),
            )
            await tx.tasks.create_task(task)
            tx.queue(EventEnvelope(
                event_id=EventId("evt_" + content_hash([str(task.task_id), "created"])),
                event_type=EventType.TASK_CREATED, run_id=RunId(stamp.run_id),
                engagement_id=binding["engagement_id"], timestamp=now,
                source="orchestration.specialist_tasks", correlation_id=stamp.run_id,
                payload={"mission_id": stamp.mission_id, "task_id": str(task.task_id)},
            ))
        return {"task_ids": [str(identities[key]) for key in ordered]}

    result = await admission.admit(stamp, proposal_id=proposal_id, proposal=payload, apply=apply)
    return result["task_ids"]
