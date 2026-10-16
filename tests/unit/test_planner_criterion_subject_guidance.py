"""Stage-one guidance keeps criterion principals within their owning question."""

from types import SimpleNamespace

import pytest

from redteam.orchestration.hypothesis_planner import _split_plan_response_schema
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanSelection,
    bind_plan_selection,
)


def test_rendered_schema_guides_criterion_principals_without_changing_fields():
    base = HypothesisPlanSelection.model_json_schema()
    rendered = _split_plan_response_schema(base)
    question_fields = set(base["$defs"]["PlannedQuestionSelection"]["properties"])
    criterion_fields = set(base["$defs"]["CompletionCriterion"]["properties"])

    assert set(rendered["$defs"]["PlannedQuestionSelection"]["properties"]) == question_fields
    assert set(rendered["$defs"]["CompletionCriterion"]["properties"]) == criterion_fields
    guidance = rendered["$defs"]["CompletionCriterion"]["properties"]["principal_refs"]
    assert "부분집합" in guidance["description"]
    assert "question의 principal_refs" in guidance["description"]
    assert "등록되고 허용" in guidance["description"]
    assert "subjects를 넓히거나 권한을 부여하지" in guidance["description"]


def test_existing_binder_rejects_registered_criterion_principal_outside_question():
    selection = HypothesisPlanSelection.model_validate({
        "updates": [],
        "questions": [{
            "question_id": "q1",
            "objective": "Inspect the response",
            "target_node_ids": ["target-1"],
            "principal_refs": ["principal-question"],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the response for this principal",
                "principal_refs": ["principal-other"],
            }],
            "candidate_refs": [],
        }],
        "reason": "Plan the scoped observation",
        "termination": "continue",
    })
    snapshot = SimpleNamespace(candidates={}, tasks={})

    with pytest.raises(AtomizationProposalError) as caught:
        bind_plan_selection(
            selection,
            snapshot,
            issued={},
            targets={"target-1"},
            principals={"principal-question", "principal-other"},
            max_tasks=8,
        )

    assert caught.value.code == "plan_question_condition_principal_outside_subjects"
    assert caught.value.diagnostic.location == (
        "$.questions[0].completion_criteria[0].principal_refs"
    )
