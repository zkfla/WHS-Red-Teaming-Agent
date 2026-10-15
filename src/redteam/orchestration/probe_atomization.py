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

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.core.contracts.completion import (
    ALLOWED_CRITERION_KINDS,
    CompletionCriterion,
    completion_condition_conflicts,
    completion_conditions,
    criterion_id,
)
from redteam.core.contracts.graph import HypothesisUpdate
from redteam.core.contracts.specialists import ProbeLimits
from redteam.core.ids import new_id
from redteam.orchestration.diagnostics import DecisionDiagnostic, safe_feedback
from redteam.orchestration.planner_context import PlannerContextRequest
from redteam.orchestration.specialist_tasks import (
    HypothesisProbeProposal,
    ProbeTaskProposal,
    ProbeTaskPurpose,
    SpecialistProposalError,
    validate_probe_subjects,
)

if TYPE_CHECKING:
    from redteam.orchestration.hypothesis_planner import PlanningSnapshot


class AtomizationProposalError(SpecialistProposalError):
    """Code-owned split-validation label; never contains model text or secrets."""

    def __init__(
        self, code: str, *, diagnostic: DecisionDiagnostic | None = None,
        condition_conflicts: tuple = (),
    ):
        super().__init__(code, condition_conflicts=condition_conflicts)
        if diagnostic is not None:
            self.diagnostic = diagnostic


def _task_rejection(code, *, index, task, field, question, criterion_ids=(),
                    differing_fields=(), repair="Preserve the supplied question contract."):
    """Bounded public references and field names, never criterion bodies or raw values."""
    location = f"$.tasks[{index}].{field}"
    references = list(criterion_ids)[:8]
    summary = safe_feedback(
        f"{repair} "
        f"Task {safe_feedback(task.task_key, 80)} for question "
        f"{safe_feedback(question.question_id, 80)}: {field} does not match the question. "
        f"Condition IDs: {', '.join(references[:1]) or 'none'}. "
        f"Differing fields: {', '.join(differing_fields) or field}.",
    )
    return AtomizationProposalError(code, diagnostic=DecisionDiagnostic(
        code=code, stage="task_atomization", kind="validation", retryable=True,
        cause_scope="model_input", owner="orchestration.probe_atomization",
        location=location, summary=summary, repair_condition=repair,
        criterion_ids=references,
        validation_issues=[{"rule": code, "location": location}],
        trace=[{"task_key": safe_feedback(task.task_key, 80),
                "question_id": safe_feedback(question.question_id, 80),
                "criterion_ids": references, "differing_fields": list(differing_fields)}],
    ))


_CONDITION_CODE = "plan_question_unknown_completion_condition"


_FOLLOWUP_SUMMARY = (
    "Source linkage is paired: give source_task_id and source_result_id together, copied "
    "from the captured Task result, or leave both null. source_question_id needs that pair."
)
_DUPLICATE_SUMMARY = (
    "Each completion criterion must be distinct within a question. Remove the repeated "
    "criterion at the location named in the diagnostic and keep one copy."
)


def _plan_rejection(code: str, *, location: str, summary: str) -> AtomizationProposalError:
    """Static, field-precise stage-1 rejection; never echoes model text or identifiers."""
    return AtomizationProposalError(code, diagnostic=DecisionDiagnostic(
        code=code, stage="hypothesis_planning", kind="validation", retryable=True,
        cause_scope="model_input", owner="orchestration.probe_atomization",
        location=location, summary=summary,
        validation_issues=[{"rule": code, "location": location}],
    ))


def _condition_rejection(*, question_index: int, condition_index: int, unknown_kind: bool):
    """Field-precise stage-1 rejection; never echoes the model's kind or description text."""
    field = "kind" if unknown_kind else "description"
    location = f"$.questions[{question_index}].completion_criteria[{condition_index}].{field}"
    if unknown_kind:
        summary = (
            "Each completion criterion kind must be one of: "
            + ", ".join(sorted(ALLOWED_CRITERION_KINDS))
            + ". Correct the kind at the location named in the diagnostic."
        )
    else:
        summary = (
            "Each completion criterion needs a nonblank description. Give the criterion at "
            "the location named in the diagnostic a nonblank description."
        )
    return AtomizationProposalError(_CONDITION_CODE, diagnostic=DecisionDiagnostic(
        code=_CONDITION_CODE, stage="hypothesis_planning", kind="validation", retryable=True,
        cause_scope="model_input", owner="orchestration.probe_atomization",
        location=location, summary=summary,
        validation_issues=[{"rule": _CONDITION_CODE, "location": location}],
    ))


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
    termination: Literal["continue", "replan", "held", "exhausted", "goal_achieved"] = "continue"
    resume_conditions: list[str] = Field(default_factory=list)
    context_request: PlannerContextRequest | None = None


class AtomizedProbeTask(ProbeTaskProposal):
    """Internal hydrated probe, never the atomizer's model-output schema."""

    question_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _binding(self):
        if not self.question_id.strip():
            raise ValueError("atomized probe needs its plan-local question_id")
        return self


class AtomizedProbeSelection(ProbeTaskPurpose):
    """Model selects whole original conditions by ID; code owns their contents."""

    question_id: str = Field(min_length=1)
    completion_criterion_ids: list[str] = Field(min_length=1)
    hypothesis_revisions: dict[str, int] = Field(
        default_factory=dict,
        description="Copy the question's hypothesis_revisions exactly: the same candidate "
                    "keys with the same revision numbers, nothing added or omitted.",
    )


class TaskAtomizationDraft(BaseModel):
    """Stage-2 judgment: probe tasks only, each referencing a plan-local question."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tasks: list[AtomizedProbeSelection] = Field(min_length=1)


def resolve_atomized_tasks(
    selections: list[AtomizedProbeSelection], *, questions: list[PlannedInvestigationQuestion],
) -> list[AtomizedProbeTask]:
    """Resolve only within each captured question; never trust rephrased condition text."""
    qmap = {question.question_id: question for question in questions}
    tasks = []
    for index, selection in enumerate(selections):
        question = qmap.get(selection.question_id)
        if question is None:
            raise AtomizationProposalError("atomized_unknown_question",
                                           diagnostic=DecisionDiagnostic(
                code="atomized_unknown_question", stage="task_atomization", kind="validation",
                location=f"$.tasks[{index}].question_id", retryable=True,
                cause_scope="model_input", owner="orchestration.probe_atomization",
                summary=f"Task {safe_feedback(selection.task_key, 80)} must select a supplied "
                        "question_id; do not invent a question.",
                validation_issues=[{"rule": "atomized_unknown_question",
                                    "location": f"$.tasks[{index}].question_id"}],
            ))
        conditions = {criterion_id(item): item for item in question.completion_criteria}
        ids = selection.completion_criterion_ids
        if len(ids) != len(set(ids)) or set(ids) - conditions.keys():
            raise _task_rejection(
                "atomized_criterion_reference_invalid", index=index, task=selection,
                field="completion_criterion_ids", question=question,
                criterion_ids=conditions.keys(), differing_fields=["completion_criterion_ids"],
                repair="Select each ID once from this question's completion_criteria. "
                       f"Allowed IDs: {', '.join(list(conditions)[:3])}.",
            )
        values = selection.model_dump(exclude={"completion_criterion_ids"})
        values["completion_criteria"] = [
            conditions[identity].model_copy(deep=True) for identity in ids
        ]
        tasks.append(AtomizedProbeTask.model_validate(values))
    return tasks


def bind_plan(
    draft: HypothesisPlanDraft,
    snapshot: PlanningSnapshot,
    *,
    issued: dict[str, str],
    targets: set[str],
    principals: set[str],
    max_tasks: int,
    existing_work_validator: Callable[[list[str], dict[str, int]], set[str]] | None = None,
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
    for question_index, question in enumerate(draft.questions):
        revisions = {issued.get(identity, identity): revision
                     for identity, revision in question.hypothesis_revisions.items()}
        if any(versions.get(identity) != revision for identity, revision in revisions.items()):
            raise AtomizationProposalError("plan_question_revision_not_in_proposal")
        if set(question.target_node_ids) - targets or set(question.principal_refs) - principals:
            raise AtomizationProposalError("plan_question_subject_not_registered")
        required = {issued.get(ref, ref) for ref in question.goal_required_hypothesis_ids}
        if required - revisions.keys() or required and not question.required:
            raise AtomizationProposalError("plan_question_required_invalid")
        if bool(question.source_task_id) != bool(question.source_result_id):
            field = "source_result_id" if question.source_task_id else "source_task_id"
            raise _plan_rejection(
                "plan_question_followup_invalid",
                location=f"$.questions[{question_index}].{field}",
                summary=_FOLLOWUP_SUMMARY,
            )
        if question.source_question_id and not question.source_task_id:
            raise _plan_rejection(
                "plan_question_followup_invalid",
                location=f"$.questions[{question_index}].source_question_id",
                summary=_FOLLOWUP_SUMMARY,
            )
        seen_criteria = set()
        for condition_index, item in enumerate(question.completion_criteria):
            identity = criterion_id(item)
            if identity in seen_criteria:
                raise _plan_rejection(
                    "plan_question_duplicate_completion_condition",
                    location=f"$.questions[{question_index}].completion_criteria[{condition_index}]",
                    summary=_DUPLICATE_SUMMARY,
                )
            seen_criteria.add(identity)
        for condition_index, condition in enumerate(question.completion_criteria):
            if not condition.is_known() or not condition.description.strip():
                raise _condition_rejection(
                    question_index=question_index, condition_index=condition_index,
                    unknown_kind=not condition.is_known(),
                )
            if set(condition.principal_refs) - set(question.principal_refs):
                raise AtomizationProposalError(
                    "plan_question_condition_principal_outside_subjects")
            hypothesis_ref = condition.hypothesis_id or ""
            if hypothesis_ref and issued.get(
                hypothesis_ref, hypothesis_ref
            ) not in revisions:
                raise AtomizationProposalError("plan_question_criterion_outside_purpose")
        resolved_criteria = [condition.model_copy(update={
            "hypothesis_id": (
                issued.get(condition.hypothesis_id, condition.hypothesis_id)
                if condition.hypothesis_id is not None else None),
        }) for condition in question.completion_criteria]
        conflicts = completion_condition_conflicts(resolved_criteria)
        if conflicts:
            raise AtomizationProposalError(
                "same_candidate_completion_condition_conflict",
                condition_conflicts=conflicts,
            )
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
            "completion_criteria": resolved_criteria,
        }))
    if set(draft.reviewed_task_ids) - snapshot.tasks.keys() or (
        set(draft.reviewed_hypothesis_ids) - snapshot.candidates.keys()
    ):
        raise AtomizationProposalError("reviewed_reference_not_in_snapshot")
    if draft.termination == "continue" and not questions:
        if existing_work_validator is None or not existing_work_validator(
            draft.reviewed_task_ids, versions,
        ):
            raise AtomizationProposalError("continue_requires_proposed_or_existing_work")
    if not (updates or questions):
        if draft.termination not in {"continue", "replan", "goal_achieved"} and (
            not draft.resume_conditions
        ):
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
    indices = {id(task): index for index, task in enumerate(tasks)}
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
            def reject(code, field, *, refs=(), fields=(), repair=None,
                       child=child, question=question):
                return _task_rejection(
                    code, index=indices[id(child)], task=child, question=question,
                    field=field, criterion_ids=refs, differing_fields=fields,
                    repair=repair or "Preserve the supplied question contract.",
                )
            if set(child.target_node_ids) - set(question.target_node_ids) or (
                set(child.principal_refs) - set(question.principal_refs)
            ):
                fields = [name for name in ("target_node_ids", "principal_refs")
                          if set(getattr(child, name)) - set(getattr(question, name))]
                raise reject("atomized_subject_outside_question", fields[0], fields=fields)
            for condition in child.completion_criteria:
                if criterion_id(condition) not in original:
                    # Internal invariant failure: retain the strict legacy comparison,
                    # reporting field names only (never the rewritten descriptions).
                    fields = ["completion_criteria"]
                    if len(question.completion_criteria) == 1:
                        expected = question.completion_criteria[0]
                        fields = [name for name in CompletionCriterion.model_fields
                                  if expected.model_dump()[name] != condition.model_dump()[name]]
                    raise reject("atomized_criterion_outside_question", "completion_criteria",
                                 refs=sorted(original), fields=fields,
                                 repair="Select the original condition ID instead of rewriting it.")
                if set(condition.principal_refs) - set(child.principal_refs):
                    raise reject("atomized_criterion_principals_outside_task", "principal_refs",
                                 refs=[criterion_id(condition)], fields=["principal_refs"],
                                 repair="Keep all principals of this whole condition on one task.")
            resolved = {issued.get(key, key): revision
                        for key, revision in child.hypothesis_revisions.items()}
            if resolved != expected_revisions:
                missing = set(expected_revisions) - set(resolved)
                extra = set(resolved) - set(expected_revisions)
                changed = {key for key in set(resolved) & set(expected_revisions)
                           if resolved[key] != expected_revisions[key]}
                fields = [label for label, items in (
                    ("missing_candidate_revisions", missing),
                    ("extra_candidate_revisions", extra),
                    ("candidate_revision_values", changed),
                ) if items]
                raise reject(
                    "atomized_revision_not_preserved", "hypothesis_revisions",
                    fields=fields,
                    repair=("Copy this question's hypothesis_revisions exactly onto each task "
                            f"of the question: expected {len(expected_revisions)} candidate "
                            f"revisions, submitted {len(resolved)}."),
                )
            if child.required != question.required or {
                issued.get(ref, ref) for ref in child.goal_required_hypothesis_ids
            } != expected_required:
                raise reject("atomized_required_not_preserved", "required",
                             fields=["required", "goal_required_hypothesis_ids"])
            if (child.source_task_id, child.source_result_id, child.source_question_id) != (
                question.source_task_id, question.source_result_id, question.source_question_id
            ):
                fields = [name for name in
                          ("source_task_id", "source_result_id", "source_question_id")
                          if getattr(child, name) != getattr(question, name)]
                raise reject("atomized_source_not_preserved", fields[0], fields=fields)
            if child.role_id not in roles:
                raise reject("probe_role_not_ready", "role_id",
                             repair="Select a currently ready executable role.")
            if bool(child.source_task_id) != bool(child.source_result_id) or (
                child.source_question_id and not child.source_task_id
            ):
                raise reject("probe_followup_requires_original_task_result_pair",
                             "source_result_id")
            if child.source_task_id:
                source = snapshot.tasks.get(child.source_task_id)
                if source is None or source.result is None or (
                    source.result.result_id != child.source_result_id
                ):
                    raise reject("probe_source_result_not_in_snapshot", "source_result_id")
            if child.probe_limits is not None and limits is not None and (
                child.probe_limits.max_actions > limits.max_actions
                or child.probe_limits.max_model_requests > limits.max_model_requests
            ):
                raise reject("probe_limits_exceed_profile", "probe_limits",
                             repair="Use limits within the supplied profile.")
        if {ref for task in children for ref in task.target_node_ids} != set(
            question.target_node_ids
        ):
            raise reject("atomized_subject_coverage_mismatch", "target_node_ids")
        if {ref for task in children for ref in task.principal_refs} != set(
            question.principal_refs
        ):
            raise reject("atomized_subject_coverage_mismatch", "principal_refs")
        retained = {criterion_id(condition) for task in children
                    for condition in task.completion_criteria}
        if original != retained:
            raise reject("atomized_criterion_coverage_mismatch", "completion_criterion_ids",
                         refs=sorted(original - retained), fields=["completion_criterion_ids"],
                         repair="The tasks together must select every original condition ID. "
                                "Add the missing condition to a task with its full "
                                "principal scope.",
                         )
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
    candidate_ids = {issued.get(ref, ref) for question in questions
                     for ref in question.hypothesis_revisions}
    candidate_ids.update(update.hypothesis.hypothesis_id for update in updates)
    candidate_ids.update(ref for update in updates
                         for ref in update.hypothesis.source_hypothesis_ids)
    task_ids = {question.source_task_id for question in questions if question.source_task_id}
    targets = {ref for question in questions for ref in question.target_node_ids}
    principals = {ref for question in questions for ref in question.principal_refs}
    evidence_ids = {ref for question in questions for ref in question.evidence_ids}
    for update in updates:
        evidence_ids.update(update.hypothesis.supporting_evidence_ids)
        evidence_ids.update(update.hypothesis.refuting_evidence_ids)
        if update.review_evidence_id:
            evidence_ids.add(update.review_evidence_id)
    tasks = [item for item in payload.get("investigations", [])
             if item["task_id"] in task_ids or (
                 item.get("status") in {"pending", "ready", "running", "blocked"}
                 and targets.intersection(item.get("related_attack_graph_node_ids", [])))]
    # The hydrated handoff contains source ancestry; retain it transitively.
    changed = True
    while changed:
        ancestors = {ref for item in tasks for ref in (
            item.get("source_task_id"), *item.get("dependency_task_ids", [])) if ref}
        missing = [item for item in payload.get("investigations", [])
                   if item["task_id"] in ancestors and item not in tasks]
        tasks.extend(missing)
        changed = bool(missing)
    evidence_ids.update(ref for item in tasks for ref in (
        *item.get("evidence_ids", []), *(item.get("result") or {}).get("evidence_ids", [])))
    related_task_ids = {item["task_id"] for item in tasks}
    # Explicit handoff: never inherit investigation history, ledger or unrelated receipts.
    selected = {key: payload[key] for key in (
        "mission", "constraints", "operator_instructions", "role_readiness", "tools_by_role",
        "tool_cards", "program_execution_environment", "program_manifest_status",
        "workroom_status", "tool_cards_total", "tool_cards_truncated", "tool_cards_guidance",
        "probe_limits", "max_tasks", "remaining_budget", "related_work", "related_executions",
    ) if key in payload}
    return {
        **selected,
        "known_target_ids": sorted(targets),
        "registered_principal_refs": sorted(principals),
        "candidates": [item for item in payload.get("candidates", [])
                       if item["hypothesis_id"] in candidate_ids],
        "investigations": tasks,
        "execution_history": [item for item in payload.get("execution_history", [])
                              if item["task_id"] in related_task_ids],
        "evidence_validity": [item for item in payload.get("evidence_validity", [])
                              if item["evidence_id"] in evidence_ids],
        "original_observations": {
            "records": [item for item in payload.get("original_observations", {}).get("records", [])
                        if item["evidence_id"] in evidence_ids],
            "note": "Bounded related original windows, not independent verdict receipts.",
        },
        "questions": [{**question.model_dump(mode="json"),
                       "completion_criteria": completion_conditions(question)}
                      for question in questions],
        "strategy": {
            "updates": strategy_updates,
            "reason": plan.reason,
            "reviewed_task_ids": list(plan.reviewed_task_ids),
            "reviewed_hypothesis_ids": list(plan.reviewed_hypothesis_ids),
            "termination": plan.termination,
            "resume_conditions": list(plan.resume_conditions),
        },
        "issued_candidate_ids": dict(issued),
        "candidate_revisions": {ref: versions[ref] for ref in sorted(candidate_ids)
                                if ref in versions},
    }
