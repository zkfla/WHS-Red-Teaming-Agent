"""Only explicit question selection authorizes copying its fixed task contract."""

import pytest
from pydantic import ValidationError

from redteam.orchestration.probe_atomization import (
    AtomizedProbeSelection,
    AtomizedProbeTask,
    resolve_atomized_tasks,
)
from tests.unit.test_probe_atomization import _question, _selection


@pytest.mark.parametrize("field,value", [
    ("hypothesis_revisions", {"other": 999}), ("required", False),
    ("goal_required_hypothesis_ids", []), ("source_task_id", "other-task"),
    ("source_result_id", "other-result"), ("source_question_id", "other-question"),
])
def test_selector_rejects_copied_contract_fields(field, value):
    selection = _selection(_question())
    assert field not in AtomizedProbeSelection.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        AtomizedProbeSelection.model_validate({**selection.model_dump(), field: value})


def test_fixed_contract_hydration_and_stored_task_roundtrip_are_isolated():
    question = _question(revisions={"candidate": 4}, required=True, goal_ids=["candidate"])
    question.source_task_id = "original-task"
    question.source_result_id = "original-result"
    question.source_question_id = "original-question"
    selection = _selection(question).model_copy(update={"required_reason": None})
    (task,) = resolve_atomized_tasks([selection], questions=[question])
    assert task.hypothesis_revisions == {"candidate": 4}
    assert task.required and task.required_reason == question.required_reason
    assert task.goal_required_hypothesis_ids == ["candidate"]
    assert (task.source_task_id, task.source_result_id, task.source_question_id) == (
        "original-task", "original-result", "original-question",
    )
    assert AtomizedProbeTask.model_validate_json(task.model_dump_json()) == task
    task.hypothesis_revisions["candidate"] = 8
    task.goal_required_hypothesis_ids.clear()
    task.completion_criteria[0].description = "changed"
    assert question.hypothesis_revisions == {"candidate": 4}
    assert question.goal_required_hypothesis_ids == ["candidate"]
    assert question.completion_criteria[0].description != "changed"


def test_explicit_task_reason_is_retained_and_blank_required_reason_rejected():
    question = _question(required=True)
    selection = _selection(question).model_copy(update={"required_reason": "Selected purpose"})
    (task,) = resolve_atomized_tasks([selection], questions=[question])
    assert task.required_reason == "Selected purpose"
    with pytest.raises(ValidationError):
        resolve_atomized_tasks(
            [selection.model_copy(update={"required_reason": " "})], questions=[question],
        )
