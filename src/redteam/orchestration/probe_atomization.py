"""Split v3 planning: investigation questions, then probe-task atomization.

Stage 1 (``hypothesis_planning``) proposes candidate updates plus investigation
QUESTIONS only -- never worker tasks. Stage 2 (``task_atomization``) decomposes
each plan-local question into probe tasks. Final admission still goes through the
single existing ``register_probe_tasks`` CAS; this module only validates the split.

Preservation rules:
- A task's subjects may be a SUBSET of its question's; the UNION over one
  question's tasks must EXACTLY cover its target_node_ids + principal_refs.
- EVERY completion criterion must be retained by at least one task, as ONE whole
  condition (criterion identity is atomic; splitting a paired-comparison
  condition changes its identity and is rejected as outside the question). A
  criterion with explicit principal_refs must land on ONE task whose own
  principal_refs include them all -- never split across tasks.
- Hypothesis revisions, the required flag/goal set and the source triple are
  preserved on EACH task. Evidence refs follow the existing registration
  admission (no new allowance).
- Plan-local question_id is only a coverage key; it never overwrites the
  original source_question_id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.graph import HypothesisUpdate
from redteam.core.contracts.specialists import ProbeLimits
from redteam.core.ids import new_id
from redteam.orchestration.specialist_tasks import (
    HypothesisProbeProposal,
    ProbeTaskProposal,
    SpecialistProposalError,
    validate_probe_subjects,
)

if TYPE_CHECKING:
    from redteam.orchestration.hypothesis_planner import PlanningSnapshot


class AtomizationProposalError(SpecialistProposalError):
    """Code-owned split-validation label; never contains model text or secrets."""
def bind_candidate_updates(
    raw_updates: list[HypothesisUpdate],
    snapshot: PlanningSnapshot,
    *,
    issued: dict[str, str],
    targets: set[str],
    principals: set[str],
    resume_conditions: list[str],
    error: type[ValueError] = AtomizationProposalError,
) -> list[HypothesisUpdate]:
    """One authority for candidate-update binding.

    Issues persistent IDs for proposal-local keys, checks expected revisions,
    subjects, sources and held-metadata. Both the legacy single-stage
    bind_decision and the split bind_plan call this; only the error label
    differs, so the codes stay identical.
    """
    bound: list[HypothesisUpdate] = []
    for update in raw_updates:
        proposed = update.hypothesis
        identity = proposed.hypothesis_id
        current = snapshot.candidates.get(identity)
        if update.expected_revision is None:
            if current is not None or proposed.status in {"confirmed", "rejected"}:
                raise error("new_candidate_requires_local_key_and_open_judgment")
            if identity not in issued:
                issued[identity] = new_id("hyp")
            identity = issued[identity]
        elif current is None or current.revision != update.expected_revision:
            raise error("candidate_expected_revision_not_in_snapshot")
        if set(proposed.target_node_ids) - targets or set(proposed.principal_refs) - principals:
            raise error("candidate_subject_not_registered")
        if set(proposed.source_hypothesis_ids) - snapshot.candidates.keys():
            raise error("candidate_sources_must_reference_existing_snapshots")
        if proposed.status == "held" and not resume_conditions:
            raise error("held_candidate_requires_resume_conditions")
        bound.append(update.model_copy(update={"hypothesis": proposed.model_copy(update={
            "hypothesis_id": identity,
            "metadata": {**proposed.metadata, **({"resume_conditions": resume_conditions}
                                              if proposed.status == "held" else {})},
        })}))
    return bound


class PlannedInvestigationQuestion(BaseModel):
    """One investigation purpose; carries meaning, never a worker role or tool order."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    question_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    target_node_ids: list[str] = Field(min_length=1)
    principal_refs: list[str] = Field(default_factory=list)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1)
    hypothesis_revisions: dict[str, int] = Field(default_factory=dict)
    required: bool = False
    required_reason: str | None = None
    goal_required_hypothesis_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    source_result_id: str | None = None
    source_question_id: str | None = None

    @model_validator(mode="after")
    def _purpose(self):
        if not self.question_id.strip() or not self.objective.strip() or any(
            not ref.strip() for ref in self.target_node_ids
        ):
            raise ValueError("planned question purpose and references must be nonblank")
        if self.required and not (self.required_reason or "").strip():
            raise ValueError("goal-required question needs an explicit reason")
        return self


class HypothesisPlanDraft(BaseModel):
    """Stage-1 judgment: candidate updates plus investigation questions, no tasks."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    updates: list[HypothesisUpdate] = Field(default_factory=list)
    questions: list[PlannedInvestigationQuestion] = Field(default_factory=list)
    reviewed_task_ids: list[str] = Field(default_factory=list)
    reviewed_hypothesis_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    termination: Literal["continue", "held", "exhausted", "goal_achieved"] = "continue"
    resume_conditions: list[str] = Field(default_factory=list)


class AtomizedProbeTask(ProbeTaskProposal):
    """A probe task bound to its plan-local question; question_id is coverage-only."""

    question_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _binding(self):
        if not self.question_id.strip():
            raise ValueError("atomized probe needs its plan-local question_id")
        return self


class TaskAtomizationDraft(BaseModel):
    """Stage-2 judgment: probe tasks only, each referencing a plan-local question."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tasks: list[AtomizedProbeTask] = Field(min_length=1)


def bind_plan(
    draft: HypothesisPlanDraft,
    snapshot: PlanningSnapshot,
    *,
    issued: dict[str, str],
    targets: set[str],
    principals: set[str],
    max_tasks: int,
) -> tuple[list[HypothesisUpdate] | None, list[PlannedInvestigationQuestion], dict[str, int]]:
    """Resolve local references for stage-1 updates+questions without mutating originals.

    Returns (updates_or_None, questions, versions). None updates marks an
    empty/held judgment that must skip the atomizer (no second request).
    versions maps persistent hypothesis IDs to the revision this pass proposes,
    including newly issued candidates at revision 1 -- the FR009 input for
    stage 2 alongside the issued-ID map itself.
    """
    names = [update.hypothesis.hypothesis_id for update in draft.updates]
    if not draft.reason.strip() or any(
        not value.strip() for value in draft.resume_conditions
    ):
        raise AtomizationProposalError("judgment_reason_or_resume_condition_blank")
    if draft.questions and draft.termination != "continue":
        raise AtomizationProposalError("new_work_requires_continue_judgment")
    if (
        len(set(names)) != len(names)
        or len({question.question_id for question in draft.questions}) != len(draft.questions)
        or len(draft.questions) > max_tasks
    ):
        raise AtomizationProposalError("candidate_identity_or_task_count_invalid")
    updates = bind_candidate_updates(
        draft.updates, snapshot, issued=issued, targets=targets,
        principals=principals, resume_conditions=draft.resume_conditions,
        error=AtomizationProposalError,
    )
    versions = {identity: candidate.revision for identity, candidate in snapshot.candidates.items()}
    versions.update({update.hypothesis.hypothesis_id: (update.expected_revision or 0) + 1
                     for update in updates})
    questions: list[PlannedInvestigationQuestion] = []
    for question in draft.questions:
        revisions = {issued.get(identity, identity): revision
                     for identity, revision in question.hypothesis_revisions.items()}
        if any(versions.get(identity) != revision for identity, revision in revisions.items()):
            raise AtomizationProposalError("plan_question_revision_not_in_proposal")
        if set(question.target_node_ids) - targets or set(question.principal_refs) - principals:
            raise AtomizationProposalError("plan_question_subject_not_registered")
        required = {issued.get(ref, ref) for ref in question.goal_required_hypothesis_ids}
        if required - revisions.keys() or required and not question.required:
            raise AtomizationProposalError("plan_question_required_invalid")
        if bool(question.source_task_id) != bool(question.source_result_id) or (
            question.source_question_id and not question.source_task_id
        ):
            raise AtomizationProposalError("plan_question_followup_invalid")
        identities = [criterion_id(item) for item in question.completion_criteria]
        if len(identities) != len(set(identities)):
            raise AtomizationProposalError("plan_question_duplicate_completion_condition")
        for condition in question.completion_criteria:
            if not condition.is_known() or not condition.description.strip():
                raise AtomizationProposalError("plan_question_unknown_completion_condition")
            if set(condition.principal_refs) - set(question.principal_refs):
                raise AtomizationProposalError(
                    "plan_question_condition_principal_outside_subjects")
            hypothesis_ref = condition.hypothesis_id or ""
            if hypothesis_ref and issued.get(
                hypothesis_ref, hypothesis_ref
            ) not in revisions:
                raise AtomizationProposalError("plan_question_criterion_outside_purpose")
        if question.source_task_id:
            source = snapshot.tasks.get(question.source_task_id)
            if source is None or source.result is None or (
                source.result.result_id != question.source_result_id
            ):
                raise AtomizationProposalError("plan_question_source_not_in_snapshot")
        questions.append(question.model_copy(update={
            "hypothesis_revisions": revisions,
            "goal_required_hypothesis_ids": [issued.get(ref, ref)
                                             for ref in question.goal_required_hypothesis_ids],
            "completion_criteria": [condition.model_copy(update={
                "hypothesis_id": (
                    issued.get(condition.hypothesis_id, condition.hypothesis_id)
                    if condition.hypothesis_id is not None else None),
            }) for condition in question.completion_criteria],
        }))
    if set(draft.reviewed_task_ids) - snapshot.tasks.keys() or (
        set(draft.reviewed_hypothesis_ids) - snapshot.candidates.keys()
    ):
        raise AtomizationProposalError("reviewed_reference_not_in_snapshot")
    if not (updates or questions):
        if draft.termination == "continue" or not draft.resume_conditions:
            raise AtomizationProposalError("empty_judgment_needs_termination_and_resume_conditions")
        return None, [], versions
    return updates, questions, versions


def needs_atomization(
    draft: HypothesisPlanDraft, questions: list[PlannedInvestigationQuestion],
) -> bool:
    """Empty/held/update-only plans skip the atomizer (no second request)."""
    return bool(questions) and draft.termination == "continue"


def validate_atomized_tasks(
    tasks: list[AtomizedProbeTask],
    *,
    questions: list[PlannedInvestigationQuestion],
    issued: dict[str, str],
    versions: dict[str, int],
    targets: set[str],
    principals: set[str],
    roles: set[str],
    limits: ProbeLimits | None,
    snapshot: PlanningSnapshot,
    max_tasks: int,
) -> list[ProbeTaskProposal]:
    """Exact question coverage with subset tasks (FR008); returns committable tasks.

    Every question_id must be covered by >=1 task; unknown question_ids are
    rejected. The union over one question's tasks must exactly cover its
    subjects and criteria; each task stays within its question. A criterion
    with explicit principal_refs must land on ONE task carrying all of them.
    """
    qmap = {question.question_id: question for question in questions}
    keys = [task.task_key for task in tasks]
    if not tasks or max_tasks < 1 or len(tasks) > max_tasks or len(set(keys)) != len(keys):
        raise AtomizationProposalError("atomized_task_count_or_identity_invalid")
    for task in tasks:
        if task.question_id not in qmap:
            raise AtomizationProposalError("atomized_unknown_question")
    grouped = {identity: [task for task in tasks if task.question_id == identity]
               for identity in qmap}
    for children in grouped.values():
        if not children:
            raise AtomizationProposalError("atomized_question_uncovered")
    for identity, children in grouped.items():
        question = qmap[identity]
        expected_revisions = {issued.get(key, key): revision
                              for key, revision in question.hypothesis_revisions.items()}
        expected_required = {issued.get(ref, ref)
                             for ref in question.goal_required_hypothesis_ids}
        original = {criterion_id(item) for item in question.completion_criteria}
        for child in children:
            if set(child.target_node_ids) - set(question.target_node_ids) or (
                set(child.principal_refs) - set(question.principal_refs)
            ):
                raise AtomizationProposalError("atomized_subject_outside_question")
            for condition in child.completion_criteria:
                if criterion_id(condition) not in original:
                    raise AtomizationProposalError("atomized_criterion_outside_question")
                if set(condition.principal_refs) - set(child.principal_refs):
                    raise AtomizationProposalError(
                        "atomized_criterion_principals_outside_task")
            resolved = {issued.get(key, key): revision
                        for key, revision in child.hypothesis_revisions.items()}
            if resolved != expected_revisions:
                raise AtomizationProposalError("atomized_revision_not_preserved")
            if child.required != question.required or {
                issued.get(ref, ref) for ref in child.goal_required_hypothesis_ids
            } != expected_required:
                raise AtomizationProposalError("atomized_required_not_preserved")
            if (child.source_task_id, child.source_result_id, child.source_question_id) != (
                question.source_task_id, question.source_result_id, question.source_question_id
            ):
                raise AtomizationProposalError("atomized_source_not_preserved")
            if child.role_id not in roles:
                raise AtomizationProposalError("probe_role_not_ready")
            if bool(child.source_task_id) != bool(child.source_result_id) or (
                child.source_question_id and not child.source_task_id
            ):
                raise AtomizationProposalError("probe_followup_requires_original_task_result_pair")
            if child.source_task_id:
                source = snapshot.tasks.get(child.source_task_id)
                if source is None or source.result is None or (
                    source.result.result_id != child.source_result_id
                ):
                    raise AtomizationProposalError("probe_source_result_not_in_snapshot")
            if child.probe_limits is not None and limits is not None and (
                child.probe_limits.max_actions > limits.max_actions
                or child.probe_limits.max_model_requests > limits.max_model_requests
            ):
                raise AtomizationProposalError("probe_limits_exceed_profile")
        if {ref for task in children for ref in task.target_node_ids} != set(
            question.target_node_ids
        ):
            raise AtomizationProposalError("atomized_subject_coverage_mismatch")
        if {ref for task in children for ref in task.principal_refs} != set(
            question.principal_refs
        ):
            raise AtomizationProposalError("atomized_subject_coverage_mismatch")
        retained = {criterion_id(condition) for task in children
                    for condition in task.completion_criteria}
        if original != retained:
            raise AtomizationProposalError("atomized_criterion_coverage_mismatch")
    stripped = [
        ProbeTaskProposal.model_validate(task.model_dump(exclude={"question_id"}))
        for task in tasks
    ]
    validate_probe_subjects(
        HypothesisProbeProposal(updates=[], tasks=stripped), known_targets=targets,
        known_principals=principals, known_hypotheses=versions.keys(),
    )
    return stripped


def build_atomizer_input(
    payload: dict,
    *,
    plan: HypothesisPlanDraft,
    updates: list[HypothesisUpdate],
    questions: list[PlannedInvestigationQuestion],
    issued: dict[str, str],
    versions: dict[str, int],
) -> dict:
    """Public structured strategy for stage 2.

    Newly issued candidates are absent from the stage-1 snapshot, so the
    atomizer gets the normalized updates (code-issued IDs, full purpose)
    plus the plan reason and refs -- never raw model text or private thoughts.
    """
    strategy_updates = []
    for update in updates:
        entry = update.model_dump(mode="json")
        # The normalized hypothesis still reads the snapshot revision for an
        # existing candidate; the prospective revision lives in versions[id].
        entry["proposed_revision"] = versions[update.hypothesis.hypothesis_id]
        strategy_updates.append(entry)
    return {
        **payload,
        "questions": [question.model_dump(mode="json") for question in questions],
        "strategy": {
            "updates": strategy_updates,
            "reason": plan.reason,
            "reviewed_task_ids": list(plan.reviewed_task_ids),
            "reviewed_hypothesis_ids": list(plan.reviewed_hypothesis_ids),
            "termination": plan.termination,
            "resume_conditions": list(plan.resume_conditions),
        },
        "issued_candidate_ids": dict(issued),
        "candidate_revisions": dict(versions),
    }
