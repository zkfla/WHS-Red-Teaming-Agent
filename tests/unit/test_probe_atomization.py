"""Split v3 atomization contract checks (sync, no harness).

Covers probe_atomization stage-1/stage-2 validation: skip rules, FR008
coverage, FR009 issued-ID forwarding, paired-principal preservation,
revision/required/source preservation, role and limit admission.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.contracts.specialists import ProbeLimits
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    AtomizedProbeTask,
    HypothesisPlanDraft,
    PlannedInvestigationQuestion,
    TaskAtomizationDraft,
    bind_plan,
    needs_atomization,
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
        {"updates": [_update()], "questions": [], "reason": "Record premise"})
    assert updates is not None and questions == [] and versions
    draft = HypothesisPlanDraft.model_validate(
        {"updates": [_update()], "questions": [], "reason": "Record premise"})
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


def test_atomization_draft_requires_question_binding():
    with pytest.raises(ValidationError):
        TaskAtomizationDraft.model_validate({"tasks": [{
            "task_key": "t1", "objective": "Probe", "title": "Probe",
            "role_id": "probe-worker", "target_node_ids": ["n1"],
            "completion_criteria": [_criterion()]}]})
