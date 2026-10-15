"""Stage-1 source linkage and duplicate criteria get field-precise, identifier-free rejections."""

from types import SimpleNamespace

import pytest

from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    bind_plan,
)
from tests.integration.test_v3_atomization import _plan_two_questions
from tests.unit.test_split_planner_repair_guidance import _advance_with

TASK_MARKER = "RAW-TASK-MARKER-3c9e"
RESULT_MARKER = "RAW-RESULT-MARKER-8d21"


def _bind(questions):
    draft = HypothesisPlanDraft.model_validate(
        {
            "updates": [],
            "questions": questions,
            "reason": "Inspect once",
        }
    )
    snapshot = SimpleNamespace(candidates={}, tasks={})
    return bind_plan(
        draft,
        snapshot,
        issued={},
        targets={"host:a"},
        principals=set(),
        max_tasks=4,
    )


def _question(**extra):
    return {
        "question_id": "q1",
        "objective": "Inspect",
        "target_node_ids": ["host:a"],
        "completion_criteria": [{"kind": "observation", "description": "Inspect"}],
        **extra,
    }


def test_task_without_result_names_result_field_without_identifiers():
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(source_task_id=TASK_MARKER)])
    diagnostic = failed.value.diagnostic
    assert failed.value.code == "plan_question_followup_invalid"
    assert diagnostic.location == "$.questions[0].source_result_id"
    assert TASK_MARKER not in repr(diagnostic)


def test_result_without_task_names_task_field_without_identifiers():
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(source_result_id=RESULT_MARKER)])
    assert failed.value.diagnostic.location == "$.questions[0].source_task_id"
    assert RESULT_MARKER not in repr(failed.value.diagnostic)


def test_source_question_without_source_pair_names_its_field():
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(source_question_id="earlier")])
    assert failed.value.code == "plan_question_followup_invalid"
    assert failed.value.diagnostic.location == "$.questions[0].source_question_id"


def test_repeated_criterion_names_second_occurrence_and_keeps_static_summary():
    criterion = {"kind": "observation", "description": "Inspect"}
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(completion_criteria=[criterion, dict(criterion)])])
    diagnostic = failed.value.diagnostic
    assert failed.value.code == "plan_question_duplicate_completion_condition"
    assert diagnostic.location == "$.questions[0].completion_criteria[1]"
    assert "Inspect" not in diagnostic.summary


async def test_malformed_source_pair_is_repaired_then_valid_plan_commits(tmp_path):
    malformed = _plan_two_questions()
    malformed["questions"][0]["source_task_id"] = TASK_MARKER
    malformed["termination"] = "continue"
    ids, spent, llm = await _advance_with(tmp_path, [malformed, _plan_two_questions()])
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    feedback = llm.plan_feedback[1]
    assert "$.questions[0].source_task_id" in feedback
    assert "plan_question_source_not_in_snapshot" in feedback
    assert "captured Task that has a result" in feedback
    assert TASK_MARKER not in feedback
