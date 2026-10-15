"""Stage-1 relation rejections name the question or criterion field at fault; repairs commit."""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.graph import Hypothesis
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    bind_plan,
)
from tests.integration.test_v3_atomization import _plan_two_questions
from tests.unit.test_plan_linkage_rejection import _question
from tests.unit.test_split_planner_repair_guidance import _advance_with

IDENTIFIER_MARKER = "RELATION-MARKER-5b7d"


def _candidate():
    return Hypothesis(
        contract_version=2,
        hypothesis_id=IDENTIFIER_MARKER,
        description="Assess the preserved response",
        reason="Assess the preserved response",
        revision=2,
        target_node_ids=["host:a"],
    )


def _criterion(**extra):
    return {"kind": "observation", "description": "Inspect", **extra}


def _bind(questions, *, principals=(), candidates=None):
    draft = HypothesisPlanDraft.model_validate(
        {"updates": [], "questions": questions, "reason": "Inspect once"}
    )
    snapshot = SimpleNamespace(candidates=candidates or {}, tasks={})
    return bind_plan(
        draft,
        snapshot,
        issued={},
        targets={"host:a"},
        principals=set(principals),
        max_tasks=4,
    )


def test_revision_mismatch_names_hypothesis_revisions_without_identifiers():
    candidates = {IDENTIFIER_MARKER: _candidate()}
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(hypothesis_revisions={IDENTIFIER_MARKER: 1})], candidates=candidates)
    diagnostic = failed.value.diagnostic
    assert failed.value.code == "plan_question_revision_not_in_proposal"
    assert diagnostic.location == "$.questions[0].hypothesis_revisions"
    assert IDENTIFIER_MARKER not in repr(diagnostic)


def test_matching_candidate_revision_binds():
    candidates = {IDENTIFIER_MARKER: _candidate()}
    _, questions, _ = _bind(
        [_question(hypothesis_revisions={IDENTIFIER_MARKER: 2})],
        candidates=candidates,
    )
    assert questions[0].hypothesis_revisions == {IDENTIFIER_MARKER: 2}


def test_criterion_candidate_outside_purpose_names_criterion_index():
    criteria = [_criterion(), _criterion(hypothesis_id=IDENTIFIER_MARKER)]
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(completion_criteria=criteria)])
    diagnostic = failed.value.diagnostic
    assert failed.value.code == "plan_question_criterion_outside_purpose"
    assert diagnostic.location == "$.questions[0].completion_criteria[1].hypothesis_id"
    assert IDENTIFIER_MARKER not in repr(diagnostic)


def test_criterion_candidate_outside_purpose_names_second_question_index():
    second = _question(
        question_id="q2",
        completion_criteria=[_criterion(hypothesis_id=IDENTIFIER_MARKER)],
    )
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([_question(), second])
    expected = "$.questions[1].completion_criteria[0].hypothesis_id"
    assert failed.value.diagnostic.location == expected


def test_condition_principal_outside_question_principals_names_criterion_index():
    criteria = [_criterion(), _criterion(principal_refs=[IDENTIFIER_MARKER])]
    question = _question(principal_refs=["A"], completion_criteria=criteria)
    with pytest.raises(AtomizationProposalError) as failed:
        _bind([question], principals={"A"})
    diagnostic = failed.value.diagnostic
    assert failed.value.code == "plan_question_condition_principal_outside_subjects"
    assert diagnostic.location == "$.questions[0].completion_criteria[1].principal_refs"
    assert IDENTIFIER_MARKER not in repr(diagnostic)


def test_condition_principal_inside_question_principals_binds():
    question = _question(
        principal_refs=["A"],
        completion_criteria=[_criterion(principal_refs=["A"])],
    )
    _, questions, _ = _bind([question], principals={"A"})
    assert questions[0].completion_criteria[0].principal_refs == ["A"]


async def test_criterion_principal_repair_then_valid_plan_commits(tmp_path):
    malformed = _plan_two_questions()
    malformed["questions"][0]["completion_criteria"][0]["principal_refs"] = [IDENTIFIER_MARKER]
    ids, spent, llm = await _advance_with(tmp_path, [malformed, _plan_two_questions()])
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    feedback = llm.plan_feedback[1]
    assert "$.questions[0].completion_criteria[0].principal_refs" in feedback
    assert IDENTIFIER_MARKER not in feedback


async def test_criterion_purpose_repair_then_valid_plan_commits(tmp_path):
    malformed = _plan_two_questions()
    malformed["questions"][0]["completion_criteria"][0]["hypothesis_id"] = IDENTIFIER_MARKER
    ids, spent, llm = await _advance_with(tmp_path, [malformed, _plan_two_questions()])
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    feedback = llm.plan_feedback[1]
    assert "$.questions[0].completion_criteria[0].hypothesis_id" in feedback
    assert IDENTIFIER_MARKER not in feedback


async def test_old_revision_map_on_wire_repair_then_valid_plan_commits(tmp_path):
    malformed = _plan_two_questions()
    # The planner wire has no revision map; the old key must be rejected as unsupported.
    malformed["questions"][0]["hypothesis_revisions"] = {IDENTIFIER_MARKER: 1}
    ids, spent, llm = await _advance_with(tmp_path, [malformed, _plan_two_questions()])
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    feedback = llm.plan_feedback[1]
    assert "schema_additionalProperties at $.questions[0]" in feedback
    assert IDENTIFIER_MARKER not in feedback
