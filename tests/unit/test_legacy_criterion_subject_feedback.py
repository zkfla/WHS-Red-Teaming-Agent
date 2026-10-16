"""Legacy specialist validation provides safe, indexed criterion repair guidance."""

import pytest

from redteam.core.contracts.completion import CompletionCriterion
from redteam.orchestration.hypothesis_planner import _proposal_validation_diagnostic
from redteam.orchestration.specialist_tasks import (
    AtomicTaskProposal,
    InvestigationQuestion,
    SpecialistProposalError,
    StrategyTasks,
    validate_strategy_tasks,
)


def _criterion(principal: str) -> CompletionCriterion:
    return CompletionCriterion(
        kind="observation",
        description="Observe the scoped response",
        principal_refs=[principal],
    )


def _task(key: str, principals: list[str], criterion_principal: str) -> AtomicTaskProposal:
    return AtomicTaskProposal(
        task_key=key,
        question_id="q1",
        objective="Inspect the response",
        title="Inspect response",
        role_id="observer",
        target_node_ids=["target-1"],
        principal_refs=principals,
        completion_criteria=[_criterion(criterion_principal)],
    )


def test_legacy_task_criterion_subject_gap_has_indexed_safe_repair_feedback():
    proposal = StrategyTasks(
        questions=[InvestigationQuestion(
            question_id="q1",
            objective="Inspect the response",
            target_node_ids=["target-1"],
            principal_refs=["principal-task"],
            completion_criteria=[_criterion("principal-task")],
        )],
        tasks=[
            _task("task-0", ["principal-task"], "principal-task"),
            _task("task-1", ["principal-task"], "principal-other"),
        ],
    )

    with pytest.raises(SpecialistProposalError) as caught:
        validate_strategy_tasks(
            proposal,
            agents=None,
            known_targets={"target-1"},
            known_principals={"principal-task", "principal-other"},
            max_tasks=4,
        )

    error = caught.value
    assert error.code == "specialist_condition_principal_outside_subjects"
    diagnostic = _proposal_validation_diagnostic(
        error,
        stage="hypothesis_planning",
        summary="Generic fallback repair text",
        schema={},
    )
    assert diagnostic.location == "$.tasks[1].completion_criteria[0].principal_refs"
    assert diagnostic.retryable is True
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.specialist_tasks"
    assert "subset" in diagnostic.summary
    assert "authorization" in diagnostic.repair_condition
    assert diagnostic.validation_issues == [{
        "rule": "specialist_condition_principal_outside_subjects",
        "location": "$.tasks[1].completion_criteria[0].principal_refs",
    }]
    rendered = repr(diagnostic)
    assert "principal-task" not in rendered
    assert "principal-other" not in rendered
