"""Stage-1 wire selection binding: candidate_refs resolve to the canonical plan.

The model-facing selection names candidates by reference only. Binding derives each
question's revision map from the authoritative snapshot and this proposal's issued IDs,
so the wire never carries a numeric revision map.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.contracts.graph import Hypothesis, HypothesisUpdate
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    HypothesisPlanSelection,
    PlannedInvestigationQuestion,
    PlannedQuestionSelection,
    bind_plan_selection,
)

TARGETS = {"n1"}
PRINCIPALS: set[str] = set()


def _hypothesis_body(local_id="local", targets=("n1",), status="open"):
    return {"contract_version": 2, "hypothesis_id": local_id,
            "description": "Access may be unguarded",
            "target_node_ids": list(targets),
            "reason": "Assess the preserved response", "status": status}


def _update(local_id="local", expected=None, targets=("n1",)):
    return {"hypothesis": _hypothesis_body(local_id, targets=targets),
            "expected_revision": expected}


def _criterion(desc="Inspect the response", principals=(), hypothesis_id=None):
    body = {"kind": "observation", "description": desc,
            "principal_refs": list(principals)}
    if hypothesis_id is not None:
        body["hypothesis_id"] = hypothesis_id
    return body


def _question(qid="q1", candidate_refs=(), criteria=None, **extra):
    body = {"question_id": qid, "objective": "Investigate " + qid,
            "target_node_ids": ["n1"],
            "completion_criteria": criteria if criteria is not None else [_criterion()],
            "candidate_refs": list(candidate_refs)}
    body.update(extra)
    return body


def _selection(questions=(), updates=(), termination="continue", reason="Plan the probe"):
    return HypothesisPlanSelection.model_validate({
        "updates": list(updates), "questions": list(questions),
        "reason": reason, "termination": termination,
    })


def _snap(candidates=None, tasks=None):
    return SimpleNamespace(candidates={
        key: value.model_copy(update={"hypothesis_id": key})
        for key, value in (candidates or {}).items()
    }, tasks=dict(tasks or {}))


def _candidate(revision):
    return Hypothesis(
        contract_version=2, hypothesis_id="fixture-candidate", description="Assess the response",
        revision=revision, target_node_ids=["n1"], reason="Assess the preserved response",
    )


def _task(result_id):
    result = None if result_id is None else SimpleNamespace(result_id=result_id)
    return SimpleNamespace(result=result)


def _bind(selection, *, snapshot, issued=None):
    return bind_plan_selection(
        selection, snapshot, issued={} if issued is None else issued,
        targets=set(TARGETS), principals=set(PRINCIPALS), max_tasks=8,
    )


def test_revisions_come_from_snapshot_expected_update_and_issued_id():
    snap = _snap(candidates={"hyp-current": _candidate(4)})
    selection = _selection(
        updates=[_update("hyp-current", expected=4), _update("new-local")],
        questions=[
            _question("q1", candidate_refs=["hyp-current", "new-local"],
                      criteria=[_criterion(hypothesis_id="new-local")]),
            _question("q2", candidate_refs=[]),
        ],
    )
    issued = {}
    draft, updates, questions, versions = _bind(selection, snapshot=snap, issued=issued)
    new_id = issued["new-local"]

    assert set(issued) == {"new-local"} and new_id != "new-local"
    assert versions == {"hyp-current": 5, new_id: 1}
    assert all(isinstance(update, HypothesisUpdate) for update in updates)
    bound = {update.hypothesis.hypothesis_id: update for update in updates}
    assert set(bound) == {"hyp-current", new_id}
    assert bound["hyp-current"].expected_revision == 4
    assert isinstance(draft, HypothesisPlanDraft)
    assert [item.question_id for item in draft.questions] == ["q1", "q2"]
    assert draft.reason == "Plan the probe"
    first, empty = questions
    assert isinstance(first, PlannedInvestigationQuestion)
    assert first.hypothesis_revisions == {"hyp-current": 5, new_id: 1}
    assert first.completion_criteria[0].hypothesis_id == new_id
    assert empty.hypothesis_revisions == {}


def test_stale_expected_revision_is_rejected():
    snap = _snap(candidates={"hyp-current": _candidate(4)})
    selection = _selection(
        updates=[_update("hyp-current", expected=3)],
        questions=[_question("q1", candidate_refs=["hyp-current"])],
    )
    with pytest.raises(AtomizationProposalError) as caught:
        _bind(selection, snapshot=snap)
    assert caught.value.code == "candidate_expected_revision_not_in_snapshot"


@pytest.mark.parametrize("refs", [
    ["SECRET-UNKNOWN-REF"],
    ["hyp-current", "hyp-current"],
    ["new-local", "hyp-issued"],
])
def test_invalid_candidate_refs_fail_closed_without_echoing_model_text(refs):
    snap = _snap(candidates={"hyp-current": _candidate(4)})
    selection = _selection(
        updates=[_update("new-local")],
        questions=[_question("q1", candidate_refs=refs)],
    )
    with pytest.raises(AtomizationProposalError) as caught:
        _bind(selection, snapshot=snap, issued={"new-local": "hyp-issued"})
    rendered = repr(caught.value) + repr(getattr(caught.value, "diagnostic", None))
    assert "SECRET-UNKNOWN-REF" not in rendered


def test_criterion_outside_question_candidate_refs_is_rejected():
    snap = _snap(candidates={"hyp-current": _candidate(4), "hyp-other": _candidate(1)})
    selection = _selection(questions=[_question(
        "q1", candidate_refs=["hyp-current"],
        criteria=[_criterion(hypothesis_id="hyp-other")],
    )])
    with pytest.raises(AtomizationProposalError) as caught:
        _bind(selection, snapshot=snap)
    assert caught.value.code == "plan_question_criterion_outside_purpose"


def test_omitted_source_result_derives_from_the_task_result():
    snap = _snap(tasks={"task-1": _task("res-1")})
    selection = _selection(questions=[
        _question("q1", source_task_id="task-1", source_question_id="src-q"),
    ])
    _, _, questions, _ = _bind(selection, snapshot=snap)
    assert questions[0].source_task_id == "task-1"
    assert questions[0].source_result_id == "res-1"
    assert questions[0].source_question_id == "src-q"


@pytest.mark.parametrize(("extra", "tasks"), [
    ({"source_task_id": "task-1", "source_result_id": None}, {"task-1": _task("res-1")}),
    ({"source_task_id": "task-1", "source_result_id": "res-other"}, {"task-1": _task("res-1")}),
    ({"source_task_id": "missing-task"}, {"task-1": _task("res-1")}),
    ({"source_task_id": "task-1"}, {"task-1": _task(None)}),
    ({"source_task_id": "task-1", "source_result_id": "res-1"}, {"task-1": _task(None)}),
])
def test_source_linkage_fails_closed(extra, tasks):
    selection = _selection(questions=[_question("q1", **extra)])
    with pytest.raises(AtomizationProposalError):
        _bind(selection, snapshot=_snap(tasks=tasks))


def test_wire_question_requires_candidate_refs_and_rejects_numeric_revision_map():
    base = _question("q1")
    without_refs = {key: value for key, value in base.items() if key != "candidate_refs"}
    with pytest.raises(ValidationError):
        PlannedQuestionSelection.model_validate(without_refs)
    with pytest.raises(ValidationError):
        PlannedQuestionSelection.model_validate({
            **base, "hypothesis_revisions": {"hyp-current": 4},
        })
    assert "hypothesis_revisions" not in PlannedQuestionSelection.model_fields
    assert PlannedQuestionSelection.model_validate(
        _question("q1", candidate_refs=[])).candidate_refs == []
    with pytest.raises(ValidationError):
        HypothesisPlanSelection.model_validate(
            {"updates": [], "questions": [], "reason": "tasks stay out of stage 1",
             "tasks": []})
    wire = _selection(updates=[_update("new-local")])
    assert all(isinstance(update, HypothesisUpdate) for update in wire.updates)


def test_canonical_questions_round_trip_through_storage_without_wire_refs():
    snap = _snap(candidates={"hyp-current": _candidate(4)}, tasks={"task-1": _task("res-1")})
    selection = _selection(
        updates=[_update("hyp-current", expected=4)],
        questions=[_question("q1", candidate_refs=["hyp-current"], source_task_id="task-1")],
    )
    draft, _, questions, _ = _bind(selection, snapshot=snap)
    stored = draft.model_dump(mode="json")
    assert "candidate_refs" not in stored["questions"][0]
    restored = HypothesisPlanDraft.model_validate(stored)
    assert restored == draft
    assert restored.questions[0].hypothesis_revisions == {"hyp-current": 5}
    question_stored = questions[0].model_dump(mode="json")
    assert PlannedInvestigationQuestion.model_validate(question_stored) == questions[0]
