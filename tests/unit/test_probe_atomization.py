"""Split v3 atomization contract checks (sync, no harness).

Covers probe_atomization stage-1/stage-2 validation: skip rules, FR008
coverage, FR009 issued-ID forwarding, paired-principal preservation,
revision/required/source preservation, role and limit admission.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.specialists import ProbeLimits
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    AtomizedProbeSelection,
    AtomizedProbeTask,
    HypothesisPlanDraft,
    HypothesisPlanSelection,
    PlannedInvestigationQuestion,
    TaskAtomizationDraft,
    bind_plan,
    bind_plan_selection,
    needs_atomization,
    resolve_atomized_tasks,
    validate_atomized_tasks,
)

TARGETS = {"n1", "n2"}
PRINCIPALS = {"A", "B"}
ROLES = {"probe-worker"}
LIMITS = ProbeLimits()
MAX_TASKS = 8

def _hypothesis_body(local_id="local", targets=("n1",), status="open"):
    return {"contract_version": 2, "hypothesis_id": local_id,
            "description": "Access may be unguarded",
            "target_node_ids": list(targets),
            "reason": "Assess the preserved response", "status": status}

def _update(local_id="local", expected=None, targets=("n1",)):
    return {"hypothesis": _hypothesis_body(local_id, targets=targets),
            "expected_revision": expected}

def _criterion(desc="Inspect the response", principals=()):
    return {"kind": "observation", "description": desc,
            "principal_refs": list(principals)}

def _question(qid="q1", targets=("n1",), principals=(), criteria=None,
              revisions=None, required=False, required_reason=None, goal_ids=()):
    body = {"question_id": qid, "objective": "Investigate " + qid,
            "target_node_ids": list(targets), "principal_refs": list(principals),
            "completion_criteria": criteria if criteria is not None else [_criterion()],
            "hypothesis_revisions": dict(revisions or {})}
    if required:
        body["required"] = True
        body["required_reason"] = required_reason or "Goal needs this answer"
        body["goal_required_hypothesis_ids"] = list(goal_ids)
    return PlannedInvestigationQuestion.model_validate(body)

def _task(key="t1", qid="q1", targets=("n1",), principals=(), criteria=None,
          role="probe-worker", revisions=None, required=False, goal_ids=()):
    body = {"task_key": key, "question_id": qid, "objective": "Probe " + key,
            "title": "Probe " + key, "role_id": role,
            "target_node_ids": list(targets), "principal_refs": list(principals),
            "completion_criteria": criteria if criteria is not None else [_criterion()],
            "hypothesis_revisions": dict(revisions or {})}
    if required:
        body["required"] = True
        body["required_reason"] = "Goal needs this answer"
        body["goal_required_hypothesis_ids"] = list(goal_ids)
    return AtomizedProbeTask.model_validate(body)

def _snap():
    return SimpleNamespace(candidates={}, tasks={})

def _bind(draft, issued=None, targets=TARGETS, principals=PRINCIPALS):
    issued = {} if issued is None else issued
    return bind_plan(HypothesisPlanDraft.model_validate(draft), _snap(),
                     issued=issued, targets=set(targets),
                     principals=set(principals), max_tasks=MAX_TASKS)


def test_plan_draft_rejects_tasks_field():
    with pytest.raises(ValidationError):
        HypothesisPlanDraft.model_validate(
            {"updates": [], "tasks": [{"task_key": "x"}],
             "reason": "tasks do not belong in stage 1"})


def test_empty_held_plan_skips_atomizer():
    updates, questions, _ = _bind(
        {"updates": [], "questions": [], "reason": "Await prerequisites",
         "termination": "held", "resume_conditions": ["An observation arrives"]})
    assert updates is None and questions == []
    draft = HypothesisPlanDraft.model_validate(
        {"updates": [], "questions": [], "reason": "Await prerequisites",
         "termination": "held", "resume_conditions": ["An observation arrives"]})
    assert needs_atomization(draft, questions) is False


def test_exhausted_plan_skips_atomizer():
    updates, questions, _ = _bind(
        {"updates": [], "questions": [], "reason": "Nothing left to ask",
         "termination": "exhausted", "resume_conditions": ["Scope expands"]})
    assert updates is None and questions == []
    draft = HypothesisPlanDraft.model_validate(
        {"updates": [], "questions": [], "reason": "Nothing left to ask",
         "termination": "exhausted", "resume_conditions": ["Scope expands"]})
    assert needs_atomization(draft, questions) is False


def test_update_only_plan_skips_atomizer():
    updates, questions, versions = _bind(
        {"updates": [_update()], "questions": [], "reason": "Record premise",
         "termination": "replan"})
    assert updates is not None and questions == [] and versions
    draft = HypothesisPlanDraft.model_validate(
        {"updates": [_update()], "questions": [], "reason": "Record premise",
         "termination": "replan"})
    assert needs_atomization(draft, questions) is False


def test_questions_require_continue_judgment():
    with pytest.raises(AtomizationProposalError,
                       match="new_work_requires_continue_judgment"):
        _bind({"updates": [], "questions": [
            _question().model_dump(mode="json")],
            "reason": "Held", "termination": "held",
            "resume_conditions": ["Something arrives"]})


def test_bind_plan_forwards_new_candidate_issued_id():
    issued = {}
    question = _question(revisions={"local": 1})
    updates, questions, versions = _bind(
        {"updates": [_update("local")],
         "questions": [question.model_dump(mode="json")],
         "reason": "Probe the new premise"}, issued=issued)
    assert set(issued) == {"local"}
    persisted = issued["local"]
    assert persisted != "local" and versions[persisted] == 1
    assert questions[0].hypothesis_revisions == {persisted: 1}


def _two_question_setup():
    q1 = _question("q1", targets=("n1",), criteria=[_criterion("Inspect n1")])
    q2 = _question("q2", targets=("n2",), criteria=[_criterion("Inspect n2")])
    _, questions, versions = _bind(
        {"updates": [], "questions": [q1.model_dump(mode="json"),
                                      q2.model_dump(mode="json")],
         "reason": "Ask two things"}, issued={})
    return questions, versions


def test_validate_union_subset_coverage_ok():
    questions, versions = _two_question_setup()
    tasks = [_task("t1", qid="q1", targets=("n1",),
                   criteria=[_criterion("Inspect n1")]),
             _task("t2a", qid="q2", targets=("n2",),
                   criteria=[_criterion("Inspect n2")]),
             _task("t2b", qid="q2", targets=("n2",),
                   criteria=[_criterion("Inspect n2")])]
    out = validate_atomized_tasks(tasks, questions=questions, issued={},
                                  versions=versions, targets=TARGETS,
                                  principals=PRINCIPALS, roles=ROLES,
                                  limits=LIMITS, snapshot=_snap(),
                                  max_tasks=MAX_TASKS)
    assert [t.task_key for t in out] == ["t1", "t2a", "t2b"]
    assert all(type(t).__name__ == "ProbeTaskProposal" for t in out)


def test_validate_rejects_uncovered_question():
    questions, versions = _two_question_setup()
    tasks = [_task("t1", qid="q1", targets=("n1",),
                   criteria=[_criterion("Inspect n1")])]
    with pytest.raises(AtomizationProposalError,
                       match="atomized_question_uncovered"):
        validate_atomized_tasks(tasks, questions=questions, issued={},
                                versions=versions, targets=TARGETS,
                                principals=PRINCIPALS, roles=ROLES,
                                limits=LIMITS, snapshot=_snap(),
                                max_tasks=MAX_TASKS)


def test_validate_rejects_unknown_question_id():
    questions, versions = _two_question_setup()
    tasks = [_task("t1", qid="q1", targets=("n1",),
                   criteria=[_criterion("Inspect n1")]),
             _task("tx", qid="nope", targets=("n1",))]
    with pytest.raises(AtomizationProposalError,
                       match="atomized_unknown_question"):
        validate_atomized_tasks(tasks, questions=questions, issued={},
                                versions=versions, targets=TARGETS,
                                principals=PRINCIPALS, roles=ROLES,
                                limits=LIMITS, snapshot=_snap(),
                                max_tasks=MAX_TASKS)


def test_validate_rejects_dropped_criterion():
    questions, versions = _two_question_setup()
    tasks = [_task("t1", qid="q1", targets=("n1",),
                   criteria=[_criterion("Something else")]),
             _task("t2", qid="q2", targets=("n2",),
                   criteria=[_criterion("Inspect n2")])]
    with pytest.raises(AtomizationProposalError,
                       match="atomized_criterion_outside_question"):
        validate_atomized_tasks(tasks, questions=questions, issued={},
                                versions=versions, targets=TARGETS,
                                principals=PRINCIPALS, roles=ROLES,
                                limits=LIMITS, snapshot=_snap(),
                                max_tasks=MAX_TASKS)


def test_paired_principals_must_ride_one_task():
    question = _question("qp", targets=("n1", "n2"), principals=("A", "B"),
                         criteria=[_criterion("Compare A vs B",
                                              principals=("A", "B"))])
    _, questions, versions = _bind(
        {"updates": [], "questions": [question.model_dump(mode="json")],
         "reason": "Paired comparison"}, issued={})
    whole = _task("t-whole", qid="qp", targets=("n1", "n2"),
                  principals=("A", "B"),
                  criteria=[_criterion("Compare A vs B",
                                       principals=("A", "B"))])
    out = validate_atomized_tasks([whole], questions=questions, issued={},
                                  versions=versions, targets=TARGETS,
                                  principals=PRINCIPALS, roles=ROLES,
                                  limits=LIMITS, snapshot=_snap(),
                                  max_tasks=MAX_TASKS)
    assert len(out) == 1


def test_validate_rejects_split_paired_principals():
    question = _question("qp", targets=("n1", "n2"), principals=("A", "B"),
                         criteria=[_criterion("Compare A vs B",
                                              principals=("A", "B"))])
    _, questions, versions = _bind(
        {"updates": [], "questions": [question.model_dump(mode="json")],
         "reason": "Paired comparison"}, issued={})
    def run(tasks):
        with pytest.raises(AtomizationProposalError):
            validate_atomized_tasks(tasks, questions=questions, issued={},
                                    versions=versions, targets=TARGETS,
                                    principals=PRINCIPALS, roles=ROLES,
                                    limits=LIMITS, snapshot=_snap(),
                                    max_tasks=MAX_TASKS)
    run([_task("t-a", qid="qp", targets=("n1",), principals=("A",),
               criteria=[_criterion("Compare A vs B", principals=("A", "B"))]),
         _task("t-b", qid="qp", targets=("n2",), principals=("B",),
               criteria=[_criterion("Compare A vs B", principals=("A", "B"))])])
    run([_task("t-a", qid="qp", targets=("n1",), principals=("A",),
               criteria=[_criterion("Compare A vs B", principals=("A",))]),
         _task("t-b", qid="qp", targets=("n2",), principals=("B",),
               criteria=[_criterion("Compare A vs B", principals=("B",))])])


def test_validate_preserves_revisions_required_and_source():
    q = _question("q1", revisions={"h1": 3}, required=True,
                  required_reason="Goal needs h1", goal_ids=["h1"])
    snap = SimpleNamespace(candidates={}, tasks={})
    versions = {"h1": 3}
    base = dict(qid="q1", targets=("n1",), revisions={"h1": 3},
                required=True, goal_ids=["h1"])
    ok = _task("t1", **base)
    out = validate_atomized_tasks([ok], questions=[q], issued={},
                                  versions=versions, targets=TARGETS,
                                  principals=PRINCIPALS, roles=ROLES,
                                  limits=LIMITS, snapshot=snap,
                                  max_tasks=MAX_TASKS)
    assert out[0].hypothesis_revisions == {"h1": 3}
    assert out[0].required is True
    bad_revision = _task("t1", **{**base, "revisions": {"h1": 2}})
    with pytest.raises(AtomizationProposalError,
                       match="atomized_revision_not_preserved"):
        validate_atomized_tasks([bad_revision], questions=[q], issued={},
                                versions=versions, targets=TARGETS,
                                principals=PRINCIPALS, roles=ROLES,
                                limits=LIMITS, snapshot=snap,
                                max_tasks=MAX_TASKS)
    bad_required = _task("t1", **{**base, "required": False, "goal_ids": []})
    with pytest.raises(AtomizationProposalError,
                       match="atomized_required_not_preserved"):
        validate_atomized_tasks([bad_required], questions=[q], issued={},
                                versions=versions, targets=TARGETS,
                                principals=PRINCIPALS, roles=ROLES,
                                limits=LIMITS, snapshot=snap,
                                max_tasks=MAX_TASKS)


def test_validate_rejects_unknown_role_and_over_limit():
    questions, versions = _two_question_setup()
    outsider = _task("t1", qid="q1", targets=("n1",), role="ghost",
                     criteria=[_criterion("Inspect n1")])
    others = [_task("t2", qid="q2", targets=("n2",),
                    criteria=[_criterion("Inspect n2")])]
    with pytest.raises(AtomizationProposalError, match="probe_role_not_ready"):
        validate_atomized_tasks([outsider, *others], questions=questions,
                                issued={}, versions=versions,
                                targets=TARGETS, principals=PRINCIPALS,
                                roles=ROLES, limits=LIMITS, snapshot=_snap(),
                                max_tasks=MAX_TASKS)
    big = _task("t1", qid="q1", targets=("n1",),
                criteria=[_criterion("Inspect n1")])
    big = big.model_copy(update={"probe_limits": LIMITS.model_copy(
        update={"max_actions": LIMITS.max_actions + 1})})
    with pytest.raises(AtomizationProposalError,
                       match="probe_limits_exceed_profile"):
        validate_atomized_tasks([big, *others], questions=questions,
                                issued={}, versions=versions,
                                targets=TARGETS, principals=PRINCIPALS,
                                roles=ROLES, limits=LIMITS, snapshot=_snap(),
                                max_tasks=MAX_TASKS)


@pytest.mark.parametrize("field", [
    "max_repair_requests", "max_consecutive_provider_failures",
])
def test_validate_rejects_task_request_sub_limits_above_profile(field):
    questions, versions = _two_question_setup()
    profile_limits = ProbeLimits(
        max_actions=4, max_model_requests=16, max_repair_requests=6,
        max_consecutive_provider_failures=6,
    )
    explicit = ProbeLimits(
        max_actions=4, max_model_requests=8, max_repair_requests=6,
        max_consecutive_provider_failures=6,
    ).model_copy(update={field: 7})
    first = _task("t1", qid="q1", targets=("n1",), criteria=[_criterion("Inspect n1")])
    first = first.model_copy(update={"probe_limits": explicit})
    second = _task("t2", qid="q2", targets=("n2",), criteria=[_criterion("Inspect n2")])

    with pytest.raises(AtomizationProposalError, match="probe_limits_exceed_profile"):
        validate_atomized_tasks(
            [first, second], questions=questions, issued={}, versions=versions,
            targets=TARGETS, principals=PRINCIPALS, roles=ROLES,
            limits=profile_limits, snapshot=_snap(), max_tasks=MAX_TASKS,
        )


def test_atomization_draft_requires_question_binding():
    with pytest.raises(ValidationError):
        TaskAtomizationDraft.model_validate({"tasks": [{
            "task_key": "t1", "objective": "Probe", "title": "Probe",
            "role_id": "probe-worker", "target_node_ids": ["n1"],
            "completion_criteria": [_criterion()]}]})


def _selection(question, *, ids=None, principals=None):
    body = _task(qid=question.question_id, targets=question.target_node_ids,
                 principals=question.principal_refs).model_dump(exclude={
                     "completion_criteria", "hypothesis_revisions", "required",
                     "goal_required_hypothesis_ids", "source_task_id",
                     "source_result_id", "source_question_id",
                 })
    body["completion_criterion_ids"] = (
        [criterion_id(item) for item in question.completion_criteria] if ids is None else ids
    )
    if principals is not None:
        body["principal_refs"] = principals
    return AtomizedProbeSelection.model_validate(body)


def _validate_resolved(selections, questions):
    return validate_atomized_tasks(
        resolve_atomized_tasks(selections, questions=questions), questions=questions,
        issued={}, versions={}, targets=TARGETS, principals=PRINCIPALS, roles=ROLES,
        limits=LIMITS, snapshot=_snap(), max_tasks=MAX_TASKS,
    )


def _selection_with_missing_hypothesis_reference(candidate_refs, *, updates=()):
    return HypothesisPlanSelection.model_validate({
        "updates": list(updates),
        "questions": [{
            "question_id": "q1", "objective": "Investigate q1", "target_node_ids": ["n1"],
            "completion_criteria": [{
                "kind": "hypothesis_confirmed", "description": "Confirm the candidate",
            }],
            "candidate_refs": list(candidate_refs),
        }],
        "reason": "Bind the candidate-specific condition", "termination": "continue",
    })


def test_unique_candidate_binds_missing_verdict_reference_before_atomizer_identity():
    selection = _selection_with_missing_hypothesis_reference(
        ["local"], updates=[_update("local")],
    )
    _, updates, questions, versions = bind_plan_selection(
        selection, _snap(), issued={}, targets=TARGETS, principals=PRINCIPALS,
        max_tasks=MAX_TASKS,
    )
    candidate_id = updates[0].hypothesis.hypothesis_id
    (question,) = questions
    criterion = question.completion_criteria[0]
    assert criterion.kind == "hypothesis_confirmed"
    assert criterion.hypothesis_id == candidate_id
    assert versions == {candidate_id: 1}
    assert updates[0].hypothesis.status == "open"

    atomized = resolve_atomized_tasks([_selection(question)], questions=questions)
    (task,) = validate_atomized_tasks(
        atomized, questions=questions, issued={}, versions=versions,
        targets=TARGETS, principals=PRINCIPALS, roles=ROLES, limits=LIMITS,
        snapshot=_snap(), max_tasks=MAX_TASKS,
    )
    assert task.completion_criteria[0].hypothesis_id == candidate_id
    assert task.hypothesis_revisions == {candidate_id: 1}
    assert criterion_id(task.completion_criteria[0]) == criterion_id(criterion)


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_missing_verdict_reference_requires_repair_when_candidate_is_not_unique(candidate_count):
    local_ids = [f"local-{index}" for index in range(candidate_count)]
    updates = [_update(identity) for identity in local_ids]
    selection = _selection_with_missing_hypothesis_reference(local_ids, updates=updates)
    with pytest.raises(AtomizationProposalError) as rejected:
        bind_plan_selection(
            selection, _snap(), issued={}, targets=TARGETS, principals=PRINCIPALS,
            max_tasks=MAX_TASKS,
        )
    diagnostic = rejected.value.diagnostic
    assert diagnostic.code == "plan_question_criterion_hypothesis_reference_required"
    assert diagnostic.location == "$.questions[0].completion_criteria[0].hypothesis_id"
    assert diagnostic.retryable is True
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.probe_atomization"


def test_initial_candidate_gate_only_applies_to_validated_continuing_investigation():
    selection = HypothesisPlanSelection.model_validate({
        "updates": [],
        "questions": [{
            "question_id": "q1", "objective": "Inspect the response",
            "target_node_ids": ["n1"], "candidate_refs": [],
            "completion_criteria": [_criterion()],
        }],
        "reason": "A target-specific investigation is proposed",
        "termination": "continue",
    })
    with pytest.raises(AtomizationProposalError, match="candidate_inventory_empty"):
        bind_plan_selection(
            selection, _snap(), issued={}, targets={"n1"}, principals=PRINCIPALS,
            max_tasks=MAX_TASKS, require_initial_candidate=True,
        )

    held = HypothesisPlanSelection.model_validate({
        "updates": [], "questions": [], "reason": "Await an external prerequisite",
        "termination": "held", "resume_conditions": ["The target becomes available"],
    })
    _, updates, questions, _ = bind_plan_selection(
        held, _snap(), issued={}, targets={"n1"}, principals=PRINCIPALS,
        max_tasks=MAX_TASKS, require_initial_candidate=True,
    )
    assert updates is None and questions == []


def test_initial_candidate_gate_does_not_mask_invalid_question_scope():
    selection = HypothesisPlanSelection.model_validate({
        "updates": [],
        "questions": [{
            "question_id": "q1", "objective": "Inspect an uncaptured response",
            "target_node_ids": ["outside"], "candidate_refs": [],
            "completion_criteria": [_criterion()],
        }],
        "reason": "A target-specific investigation is proposed",
        "termination": "continue",
    })
    with pytest.raises(AtomizationProposalError, match="plan_question_subject_not_registered"):
        bind_plan_selection(
            selection, _snap(), issued={}, targets={"n1"}, principals=PRINCIPALS,
            max_tasks=MAX_TASKS, require_initial_candidate=True,
        )


def test_explicit_verdict_reference_outside_question_candidates_still_rejected():
    selection = _selection_with_missing_hypothesis_reference(
        ["local"], updates=[_update("local")],
    )
    question = selection.questions[0].model_copy(update={
        "completion_criteria": [CompletionCriterion(
            kind="hypothesis_confirmed", description="Confirm the candidate",
            hypothesis_id="unlinked",
        )],
    })
    selection = selection.model_copy(update={"questions": [question]})
    with pytest.raises(AtomizationProposalError) as rejected:
        bind_plan_selection(
            selection, _snap(), issued={}, targets=TARGETS, principals=PRINCIPALS,
            max_tasks=MAX_TASKS,
        )
    diagnostic = rejected.value.diagnostic
    assert diagnostic.code == "plan_question_criterion_outside_purpose"
    assert diagnostic.location == "$.questions[0].completion_criteria[0].hypothesis_id"
    assert diagnostic.retryable is True


def test_reference_output_excludes_rewritten_conditions_and_restores_whole_original():
    condition = {**_criterion("Compare both preserved responses exactly", ("A", "B")),
                 "min_evidence": 3}
    question = _question(principals=("A", "B"), criteria=[condition])
    selection = _selection(question)
    schema = AtomizedProbeSelection.model_json_schema()
    assert "completion_criteria" not in schema["properties"]
    assert "completion_criterion_ids" in schema["required"]
    with pytest.raises(ValidationError):
        AtomizedProbeSelection.model_validate({**selection.model_dump(),
                                              "completion_criteria": [condition]})
    (task,) = _validate_resolved([selection], [question])
    assert task.completion_criteria[0].model_dump() == question.completion_criteria[0].model_dump()
    task.completion_criteria[0].description = "mutated copy"
    assert question.completion_criteria[0].description == condition["description"]


@pytest.mark.parametrize("mode", ["unknown", "other_question", "duplicate"])
def test_condition_selection_is_question_scoped_and_reports_public_repair(mode):
    question, other = _question(), _question("q2", criteria=[_criterion("Other condition")])
    valid = criterion_id(question.completion_criteria[0])
    ids = {"unknown": ["PRIVATE-INVALID-REFERENCE"],
           "other_question": [criterion_id(other.completion_criteria[0])],
           "duplicate": [valid, valid]}[mode]
    with pytest.raises(AtomizationProposalError) as rejected:
        resolve_atomized_tasks([_selection(question, ids=ids)], questions=[question, other])
    diagnostic = rejected.value.diagnostic
    assert diagnostic.location == "$.tasks[0].completion_criterion_ids"
    assert diagnostic.cause_scope == "model_input" and diagnostic.retryable
    assert diagnostic.criterion_ids == [valid]
    assert {"task_key": "t1", "question_id": "q1"}.items() <= diagnostic.trace[0].items()
    assert "completion_criterion_ids" in diagnostic.feedback()
    assert valid in diagnostic.feedback()
    assert "PRIVATE-INVALID-REFERENCE" not in diagnostic.model_dump_json()


def test_reference_selection_does_not_allow_missing_condition_or_split_principal_scope():
    question = _question(criteria=[_criterion("First"), _criterion("Second")])
    first, second = [criterion_id(item) for item in question.completion_criteria]
    with pytest.raises(AtomizationProposalError) as rejected:
        _validate_resolved([_selection(question, ids=[first])], [question])
    assert rejected.value.code == "atomized_criterion_coverage_mismatch"
    assert rejected.value.diagnostic.criterion_ids == [second]
    paired = _question(principals=("A", "B"),
                       criteria=[_criterion("Compare both", ("A", "B"))])
    with pytest.raises(AtomizationProposalError) as rejected:
        _validate_resolved([_selection(paired, principals=["A"])], [paired])
    assert rejected.value.code == "atomized_criterion_principals_outside_task"
    assert rejected.value.diagnostic.location == "$.tasks[0].principal_refs"
    assert rejected.value.diagnostic.trace[0]["differing_fields"] == ["principal_refs"]


def test_internal_condition_mismatch_reports_fields_without_condition_text():
    question = _question(criteria=[_criterion("Original private description")])
    changed = _task(criteria=[_criterion("Changed private description")])
    with pytest.raises(AtomizationProposalError) as rejected:
        validate_atomized_tasks([changed], questions=[question], issued={}, versions={},
                               targets=TARGETS, principals=PRINCIPALS, roles=ROLES,
                               limits=LIMITS, snapshot=_snap(), max_tasks=MAX_TASKS)
    diagnostic = rejected.value.diagnostic
    assert diagnostic.trace[0]["differing_fields"] == ["description"]
    assert diagnostic.criterion_ids == [criterion_id(question.completion_criteria[0])]
    assert "description" in diagnostic.feedback()
    assert "private description" not in diagnostic.model_dump_json()
