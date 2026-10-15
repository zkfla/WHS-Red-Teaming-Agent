"""Candidate-subject preflight: predicate, gate order, legacy binding and repair feedback."""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.graph import HypothesisUpdate
from redteam.orchestration.hypothesis_planner import (
    HypothesisDecision,
    HypothesisProposalError,
    _proposal_repair_summary,
    _proposal_validation_diagnostic,
    bind_decision,
)
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    PlannedInvestigationQuestion,
    _bind_plan_questions,
    bind_plan,
)
from redteam.orchestration.specialist_tasks import ProbeTaskProposal

TARGETS = {"n1", "n2", "n3"}
PRINCIPALS = {"P1", "P2", "P3"}


def _body(local_id, targets, principals=(), status="open"):
    return {"contract_version": 2, "hypothesis_id": local_id,
            "description": "Access may be unguarded",
            "target_node_ids": list(targets), "principal_refs": list(principals),
            "reason": "Assess the preserved response", "status": status}


def _update(local_id, expected=None, targets=("n1",), principals=()):
    return HypothesisUpdate.model_validate(
        {"hypothesis": _body(local_id, targets, principals), "expected_revision": expected})


def _candidate(revision, targets, principals=()):
    hypothesis = _update("hyp-a", targets=targets, principals=principals).hypothesis
    return hypothesis.model_copy(update={"revision": revision})


def _snap(candidates):
    return SimpleNamespace(candidates=dict(candidates), tasks={})


def _question(qid, targets, revisions, principals=(), **extra):
    return {"question_id": qid, "objective": "Investigate " + qid,
            "target_node_ids": list(targets), "principal_refs": list(principals),
            "completion_criteria": [{"kind": "observation", "description": "Inspect the response"}],
            "hypothesis_revisions": dict(revisions), **extra}


def _bind_stage_one(snapshot, questions, updates=()):
    draft = HypothesisPlanDraft.model_validate({
        "updates": list(updates), "questions": questions,
        "reason": "Plan the probe", "termination": "continue",
    })
    return bind_plan(draft, snapshot, issued={}, targets=set(TARGETS),
                     principals=set(PRINCIPALS), max_tasks=8)


def _task(key, targets, revisions, principals=()):
    return ProbeTaskProposal.model_validate({
        "task_key": key, "objective": "Inspect the response", "title": "Inspect",
        "role_id": "probe_role", "target_node_ids": list(targets),
        "principal_refs": list(principals), "evidence_ids": [],
        "hypothesis_revisions": dict(revisions),
        "completion_criteria": [{"kind": "observation", "description": "Inspect the response"}],
    })


def _bind_legacy(snapshot, tasks, updates=()):
    decision = HypothesisDecision(updates=list(updates), tasks=list(tasks),
                                  reason="Plan the probe", termination="continue")
    return bind_decision(decision, snapshot, issued={}, targets=set(TARGETS),
                         principals=set(PRINCIPALS), roles={"probe_role"}, max_tasks=8,
                         limits=None)


def test_predicate_reports_target_then_principal_only_for_real_gaps():
    hypothesis = _candidate(1, ["n1"], ["P1"])
    assert hypothesis.probe_subject_mismatches(["n1"], ["P1"]) == ()
    assert hypothesis.probe_subject_mismatches([], []) == ()
    assert hypothesis.probe_subject_mismatches(["n2"], []) == ("target",)
    assert hypothesis.probe_subject_mismatches([], ["P2"]) == ("principal",)
    assert hypothesis.probe_subject_mismatches(["n2"], ["P2"]) == ("target", "principal")


def test_stage_one_subject_gate_runs_before_other_question_checks():
    snap = _snap({"hyp-a": _candidate(1, ["n1"])})
    question = _question("q1", ["n2"], {"hyp-a": 1}, required=True,
                         required_reason="Needed for the goal",
                         goal_required_hypothesis_ids=["hyp-missing"])
    with pytest.raises(AtomizationProposalError) as caught:
        _bind_stage_one(snap, [question])
    assert caught.value.code == "plan_question_target_outside_candidate"


def test_stage_one_principal_gap_is_reported_on_principal_field():
    snap = _snap({"hyp-a": _candidate(1, ["n1"], ["P1"])})
    with pytest.raises(AtomizationProposalError) as caught:
        _bind_stage_one(snap, [_question("q1", ["n1"], {"hyp-a": 1}, ["P2"])])
    assert caught.value.code == "plan_question_principal_outside_candidate"
    assert caught.value.diagnostic.location == "$.questions[0].principal_refs"


def test_stage_one_repair_feedback_is_retryable_and_names_no_model_values():
    snap = _snap({"hyp-a": _candidate(1, ["n1"])})
    with pytest.raises(AtomizationProposalError) as caught:
        _bind_stage_one(snap, [_question("q1", ["n2"], {"hyp-a": 1})])
    diagnostic = _proposal_validation_diagnostic(
        caught.value, stage="hypothesis_planning",
        summary=_proposal_repair_summary(caught.value, "fallback"), schema={},
    )
    assert diagnostic.code == "plan_question_target_outside_candidate"
    assert diagnostic.retryable is True
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.probe_atomization"
    assert diagnostic.location == "$.questions[0].target_node_ids"
    assert "hypothesis_revisions" in diagnostic.summary
    assert "candidate_refs" not in diagnostic.summary
    rendered = repr(diagnostic)
    assert "hyp-a" not in rendered and "n2" not in rendered


def test_subject_summary_names_the_relation_field_of_each_path():
    question = PlannedInvestigationQuestion.model_validate(_question("q1", ["n2"], {"hyp-a": 1}))
    with pytest.raises(AtomizationProposalError) as caught:
        _bind_plan_questions(
            [question], _snap({"hyp-a": _candidate(1, ["n1"])}), issued={},
            targets=set(TARGETS), principals=set(PRINCIPALS), versions={"hyp-a": 1},
            candidate_views={"hyp-a": _candidate(1, ["n1"])}, relation_field="candidate_refs",
        )
    assert "candidate_refs" in caught.value.diagnostic.summary


def test_missing_candidate_view_fails_closed_as_internal_invariant():
    question = PlannedInvestigationQuestion.model_validate(_question("q1", ["n1"], {"hyp-a": 1}))
    with pytest.raises(AtomizationProposalError) as caught:
        _bind_plan_questions(
            [question], _snap({"hyp-a": _candidate(1, ["n1"])}), issued={},
            targets=set(TARGETS), principals=set(PRINCIPALS), versions={"hyp-a": 1},
            candidate_views={},
        )
    diagnostic = caught.value.diagnostic
    assert caught.value.code == "candidate_subject_view_missing"
    assert diagnostic.kind == "internal"
    assert diagnostic.retryable is False
    assert diagnostic.cause_scope == "runtime_contract"
    assert diagnostic.owner == "orchestration.probe_atomization"
    assert diagnostic.location == "$.questions[0].hypothesis_revisions"


def test_legacy_task_outside_candidate_is_a_typed_indexed_rejection():
    snap = _snap({"hyp-a": _candidate(4, ["n1"])})
    with pytest.raises(HypothesisProposalError) as caught:
        _bind_legacy(snap, [_task("t1", ["n2"], {"hyp-a": 4})])
    assert caught.value.code == "probe_target_outside_candidate"
    diagnostic = caught.value.diagnostic
    assert diagnostic.location == "$.tasks[0].target_node_ids"
    assert diagnostic.retryable is True
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.hypothesis_planner"


def test_legacy_principal_gap_uses_the_task_index_and_principal_field():
    snap = _snap({"hyp-a": _candidate(1, ["n1"], ["P1"])})
    tasks = [_task("t0", ["n1"], {"hyp-a": 1}, ["P1"]), _task("t1", ["n1"], {"hyp-a": 1}, ["P2"])]
    with pytest.raises(HypothesisProposalError) as caught:
        _bind_legacy(snap, tasks)
    assert caught.value.code == "probe_principal_outside_candidate"
    diagnostic = caught.value.diagnostic
    assert diagnostic.location == "$.tasks[1].principal_refs"
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.hypothesis_planner"


def test_legacy_task_uses_post_update_candidate_subjects():
    snap = _snap({"hyp-a": _candidate(4, ["n1"])})
    proposal = _bind_legacy(
        snap, [_task("t1", ["n2"], {"hyp-a": 5})],
        updates=[_update("hyp-a", expected=4, targets=("n2",))],
    )
    assert proposal is not None
    assert proposal.tasks[0].hypothesis_revisions == {"hyp-a": 5}


def test_legacy_repair_feedback_carries_indexed_field_and_model_input_scope():
    snap = _snap({"hyp-a": _candidate(4, ["n1"])})
    with pytest.raises(HypothesisProposalError) as caught:
        _bind_legacy(snap, [_task("t1", ["n2"], {"hyp-a": 4})])
    diagnostic = _proposal_validation_diagnostic(
        caught.value, stage="hypothesis_planning",
        summary=_proposal_repair_summary(caught.value, "fallback"), schema={},
    )
    assert diagnostic.code == "probe_target_outside_candidate"
    assert diagnostic.retryable is True
    assert diagnostic.location == "$.tasks[0].target_node_ids"
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.hypothesis_planner"
    assert "Narrow the task" in diagnostic.summary
