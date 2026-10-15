"""Stage-1 completion-criterion rejections name the failing field; a corrected plan then commits."""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.completion import ALLOWED_CRITERION_KINDS
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    bind_plan,
)
from tests.integration.test_v3_atomization import _plan_two_questions
from tests.unit.test_split_planner_repair_guidance import _advance_with


def _blank_description_plan():
    plan = _plan_two_questions()
    plan["reason"] = "RAW-PLAN-REASON-MARKER-4a9c"
    plan["questions"][0]["completion_criteria"][0]["description"] = "   "
    return plan


async def test_blank_description_is_rejected_at_its_field_then_corrected_plan_commits(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path,
        [_blank_description_plan(), _plan_two_questions()],
    )
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    feedback = llm.plan_feedback[1]
    assert "$.questions[0].completion_criteria[0].description" in feedback
    assert "nonblank description" in feedback
    assert "$.questions[0].completion_criteria[0].kind" not in feedback
    assert "RAW-PLAN-REASON-MARKER-4a9c" not in feedback


def test_unknown_kind_is_rejected_at_its_field_and_lists_allowed_kinds():
    draft = HypothesisPlanDraft.model_validate(
        {
            "updates": [],
            "questions": [
                {
                    "question_id": "q1",
                    "objective": "Inspect",
                    "target_node_ids": ["host:a"],
                    "completion_criteria": [{"kind": "not_a_real_kind", "description": "Inspect"}],
                }
            ],
            "reason": "Inspect once",
        }
    )
    snapshot = SimpleNamespace(candidates={}, tasks={})
    with pytest.raises(AtomizationProposalError) as failed:
        bind_plan(draft, snapshot, issued={}, targets={"host:a"}, principals=set(), max_tasks=4)
    diagnostic = failed.value.diagnostic
    assert failed.value.code == "plan_question_unknown_completion_condition"
    assert diagnostic.location == "$.questions[0].completion_criteria[0].kind"
    assert all(kind in diagnostic.summary for kind in ALLOWED_CRITERION_KINDS)
    assert "not_a_real_kind" not in diagnostic.summary
