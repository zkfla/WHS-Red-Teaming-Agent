"""Stage-1 follow-up and candidate-ref repair text: code-owned, static, identifier-free."""

from types import SimpleNamespace

import pytest

from redteam.orchestration.probe_atomization import (
    _FOLLOWUP_SUMMARY,
    _SOURCE_TASK_SUMMARY,
    _UNKNOWN_CANDIDATE_REF_SUMMARY,
    _WIRE_FOLLOWUP_SUMMARY,
    AtomizationProposalError,
    HypothesisPlanDraft,
    bind_plan,
)
from tests.unit.test_stage1_selection_binding import _bind, _question, _selection, _snap, _task

REF_MARKER = "RAW-REF-MARKER-7e03"


def _rejected(selection, snapshot):
    with pytest.raises(AtomizationProposalError) as failed:
        _bind(selection, snapshot=snapshot)
    return failed.value


def test_explicit_null_result_for_captured_source_gets_omit_guidance():
    error = _rejected(
        _selection(questions=[_question("q1", source_task_id="task-1", source_result_id=None)]),
        _snap(tasks={"task-1": _task("res-1")}),
    )
    assert error.code == "plan_question_followup_invalid"
    assert error.diagnostic.location == "$.questions[0].source_result_id"
    assert error.diagnostic.summary == _WIRE_FOLLOWUP_SUMMARY
    assert "omit source_result_id" in error.diagnostic.summary
    assert "do not send null" in error.diagnostic.summary
    rendered = repr(error.diagnostic)
    assert "task-1" not in rendered and "res-1" not in rendered


@pytest.mark.parametrize("tasks", [{}, {"task-1": _task(None)}])
def test_unknown_or_resultless_source_task_is_named_before_pair_guidance(tasks):
    error = _rejected(
        _selection(questions=[_question("q1", source_task_id="task-1")]),
        _snap(tasks=tasks),
    )
    assert error.code == "plan_question_source_not_in_snapshot"
    assert error.diagnostic.location == "$.questions[0].source_task_id"
    assert error.diagnostic.summary == _SOURCE_TASK_SUMMARY
    assert "task-1" not in repr(error.diagnostic)


def test_canonical_explicit_null_keeps_paired_guidance():
    draft = HypothesisPlanDraft.model_validate({
        "updates": [],
        "questions": [{
            "question_id": "q1", "objective": "Inspect", "target_node_ids": ["n1"],
            "completion_criteria": [{"kind": "observation", "description": "Inspect"}],
            "source_task_id": "task-1", "source_result_id": None,
        }],
        "reason": "Inspect once",
    })
    snapshot = SimpleNamespace(candidates={}, tasks={"task-1": _task("res-1")})
    with pytest.raises(AtomizationProposalError) as failed:
        bind_plan(draft, snapshot, issued={}, targets={"n1"}, principals=set(), max_tasks=4)
    assert failed.value.code == "plan_question_followup_invalid"
    assert failed.value.diagnostic.summary == _FOLLOWUP_SUMMARY
    assert _FOLLOWUP_SUMMARY != _WIRE_FOLLOWUP_SUMMARY


def test_unknown_candidate_ref_is_static_and_names_the_allowed_forms():
    error = _rejected(
        _selection(questions=[_question("q1", candidate_refs=[REF_MARKER])]),
        _snap(),
    )
    assert error.code == "plan_question_candidate_ref_unknown"
    assert error.diagnostic.summary == _UNKNOWN_CANDIDATE_REF_SUMMARY
    assert REF_MARKER not in repr(error.diagnostic)
    assert "[]" in _UNKNOWN_CANDIDATE_REF_SUMMARY
    assert "local hypothesis_id" in _UNKNOWN_CANDIDATE_REF_SUMMARY


def test_empty_candidate_refs_bind_without_any_candidate_update():
    _, updates, questions, versions = _bind(
        _selection(questions=[_question("q1", candidate_refs=[])]), snapshot=_snap(),
    )
    assert questions[0].hypothesis_revisions == {}
    assert not updates and versions == {}


@pytest.mark.parametrize("guidance", [
    _WIRE_FOLLOWUP_SUMMARY, _SOURCE_TASK_SUMMARY, _UNKNOWN_CANDIDATE_REF_SUMMARY,
])
def test_repair_guidance_is_bounded_plain_text(guidance):
    assert len(guidance) <= 500
    assert "{" not in guidance and "}" not in guidance