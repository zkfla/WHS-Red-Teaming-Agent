"""Stage-1 candidate subject alignment: each question fits inside every candidate it names.

Central admission gives every atomized task all candidate IDs of its question, so a
question's targets and principals must be a subset of EACH referenced candidate's targets
and principals. Revision checks run first. The wire path (bind_plan_selection) and the
canonical path (bind_plan) apply the same gate with typed, field-precise diagnostics.
"""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.graph import Hypothesis
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    HypothesisPlanSelection,
    bind_plan,
    bind_plan_selection,
)

TARGETS = {"n1", "n2", "n3"}
PRINCIPALS = {"P1", "P2", "P3", "P9", "SECRET-PRINCIPAL-ZZ"}
PATHS = ("selection", "canonical")
CODES = {
    "target_node_ids": "plan_question_target_outside_candidate",
    "principal_refs": "plan_question_principal_outside_candidate",
}
NEW_KEY = "new-local"
NEW_ID = "hyp-new"
ISSUED = {NEW_KEY: NEW_ID}


def _hypothesis_body(local_id, targets, principals=(), status="open"):
    return {"contract_version": 2, "hypothesis_id": local_id,
            "description": "Access may be unguarded",
            "target_node_ids": list(targets), "principal_refs": list(principals),
            "reason": "Assess the preserved response", "status": status}


def _update(local_id, expected=None, targets=("n1",), principals=()):
    return {"hypothesis": _hypothesis_body(local_id, targets, principals),
            "expected_revision": expected}


def _candidate(revision, targets, principals=()):
    return Hypothesis(
        contract_version=2, hypothesis_id="fixture-candidate", description="Assess the response",
        revision=revision, target_node_ids=list(targets), principal_refs=list(principals),
        reason="Assess the preserved response",
    )


def _snap(candidates):
    return SimpleNamespace(candidates={
        key: value.model_copy(update={"hypothesis_id": key})
        for key, value in candidates.items()
    }, tasks={})


def _question(qid, targets, refs=(), principals=()):
    return {"qid": qid, "targets": list(targets), "principals": list(principals),
            "refs": list(refs)}


def _versions(snapshot, updates):
    versions = {key: candidate.revision for key, candidate in snapshot.candidates.items()}
    for raw in updates:
        local = raw["hypothesis"]["hypothesis_id"]
        expected = raw["expected_revision"]
        if expected is None:
            versions[ISSUED.get(local, local)] = 1
        else:
            versions[local] = expected + 1
    return versions


def _run(path, *, snapshot, questions, updates=()):
    issued = dict(ISSUED)
    if path == "selection":
        selection = HypothesisPlanSelection.model_validate({
            "updates": list(updates),
            "questions": [{
                "question_id": q["qid"], "objective": "Investigate " + q["qid"],
                "target_node_ids": q["targets"], "principal_refs": q["principals"],
                "completion_criteria": [{"kind": "observation",
                                         "description": "Inspect the response"}],
                "candidate_refs": q["refs"],
            } for q in questions],
            "reason": "Plan the probe", "termination": "continue",
        })
        return bind_plan_selection(
            selection, snapshot, issued=issued, targets=set(TARGETS),
            principals=set(PRINCIPALS), max_tasks=8,
        )[2]
    versions = _versions(snapshot, updates)
    draft = HypothesisPlanDraft.model_validate({
        "updates": list(updates),
        "questions": [{
            "question_id": q["qid"], "objective": "Investigate " + q["qid"],
            "target_node_ids": q["targets"], "principal_refs": q["principals"],
            "completion_criteria": [{"kind": "observation",
                                     "description": "Inspect the response"}],
            "hypothesis_revisions": {
                ISSUED.get(ref, ref): versions[ISSUED.get(ref, ref)] for ref in q["refs"]
            },
        } for q in questions],
        "reason": "Plan the probe", "termination": "continue",
    })
    return bind_plan(
        draft, snapshot, issued=issued, targets=set(TARGETS),
        principals=set(PRINCIPALS), max_tasks=8,
    )[1]


def _assert_subject_rejection(error, field):
    assert error.code == CODES[field]
    diagnostic = error.diagnostic
    assert diagnostic.retryable is True
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "orchestration.probe_atomization"
    assert diagnostic.stage == "hypothesis_planning"
    assert diagnostic.kind == "validation"
    assert diagnostic.location.endswith(field)


def _assert_redacted(error, *raw_values):
    rendered = repr(error) + repr(getattr(error, "diagnostic", None))
    for value in raw_values:
        assert value not in rendered


@pytest.mark.parametrize("path", PATHS)
def test_unchanged_candidate_target_mismatch_is_typed_retryable_field_diagnostic(path):
    snap = _snap({"hyp-subj-a": _candidate(4, ["n1"])})
    with pytest.raises(AtomizationProposalError) as caught:
        _run(path, snapshot=snap, questions=[_question("q1", ["n2"], refs=["hyp-subj-a"])])
    _assert_subject_rejection(caught.value, "target_node_ids")
    _assert_redacted(caught.value, "hyp-subj-a")


@pytest.mark.parametrize("path", PATHS)
def test_proposed_update_subjects_override_the_snapshot(path):
    snap = _snap({"hyp-subj-a": _candidate(4, ["n1"])})
    updates = [_update("hyp-subj-a", expected=4, targets=("n2",))]
    questions = _run(path, snapshot=snap, updates=updates,
                     questions=[_question("q1", ["n2"], refs=["hyp-subj-a"])])
    assert questions[0].hypothesis_revisions == {"hyp-subj-a": 5}
    with pytest.raises(AtomizationProposalError) as caught:
        _run(path, snapshot=snap, updates=updates,
             questions=[_question("q1", ["n1"], refs=["hyp-subj-a"])])
    _assert_subject_rejection(caught.value, "target_node_ids")


@pytest.mark.parametrize("path", PATHS)
def test_issued_new_candidate_binds_its_own_subjects(path):
    updates = [_update(NEW_KEY, targets=("n2",), principals=("P1",))]
    questions = _run(path, snapshot=_snap({}), updates=updates,
                     questions=[_question("q1", ["n2"], refs=[NEW_KEY], principals=["P1"])])
    assert questions[0].hypothesis_revisions == {NEW_ID: 1}
    with pytest.raises(AtomizationProposalError) as target_caught:
        _run(path, snapshot=_snap({}), updates=updates,
             questions=[_question("q1", ["n1"], refs=[NEW_KEY])])
    _assert_subject_rejection(target_caught.value, "target_node_ids")
    with pytest.raises(AtomizationProposalError) as principal_caught:
        _run(path, snapshot=_snap({}), updates=updates,
             questions=[_question("q1", ["n2"], refs=[NEW_KEY], principals=["P2"])])
    _assert_subject_rejection(principal_caught.value, "principal_refs")


@pytest.mark.parametrize("path", PATHS)
def test_disjoint_candidate_subjects_cannot_jointly_cover_a_question(path):
    snap = _snap({"hyp-subj-a": _candidate(1, ["n1"]), "hyp-subj-b": _candidate(1, ["n2"])})
    single = _run(path, snapshot=snap,
                  questions=[_question("q1", ["n1"], refs=["hyp-subj-a"])])
    assert single[0].hypothesis_revisions == {"hyp-subj-a": 1}
    with pytest.raises(AtomizationProposalError) as caught:
        _run(path, snapshot=snap,
             questions=[_question("q1", ["n1", "n2"], refs=["hyp-subj-a", "hyp-subj-b"])])
    _assert_subject_rejection(caught.value, "target_node_ids")
    with pytest.raises(AtomizationProposalError) as caught_one:
        _run(path, snapshot=snap,
             questions=[_question("q1", ["n1"], refs=["hyp-subj-a", "hyp-subj-b"])])
    _assert_subject_rejection(caught_one.value, "target_node_ids")


@pytest.mark.parametrize("path", PATHS)
def test_question_inside_every_candidate_intersection_is_accepted(path):
    snap = _snap({
        "hyp-subj-a": _candidate(1, ["n1", "n2"], ["P1", "P2"]),
        "hyp-subj-b": _candidate(2, ["n1", "n2", "n3"], ["P1", "P2", "P3"]),
    })
    questions = _run(path, snapshot=snap, questions=[
        _question("q1", ["n1", "n2"], refs=["hyp-subj-a", "hyp-subj-b"], principals=["P1"]),
    ])
    assert questions[0].hypothesis_revisions == {"hyp-subj-a": 1, "hyp-subj-b": 2}


@pytest.mark.parametrize("path", PATHS)
def test_anonymous_question_principal_is_valid_for_principal_bound_candidate(path):
    snap = _snap({"hyp-subj-a": _candidate(1, ["n1"], ["P1"])})
    questions = _run(path, snapshot=snap, questions=[
        _question("q1", ["n1"], refs=["hyp-subj-a"], principals=[]),
    ])
    assert questions[0].principal_refs == []


@pytest.mark.parametrize("path", PATHS)
def test_wrong_principal_is_rejected_without_echoing_model_text(path):
    snap = _snap({"hyp-subj-a": _candidate(1, ["n1"], ["P1"])})
    with pytest.raises(AtomizationProposalError) as caught:
        _run(path, snapshot=snap, questions=[
            _question("q1", ["n1"], refs=["hyp-subj-a"], principals=["SECRET-PRINCIPAL-ZZ"]),
        ])
    _assert_subject_rejection(caught.value, "principal_refs")
    _assert_redacted(caught.value, "SECRET-PRINCIPAL-ZZ", "hyp-subj-a")


@pytest.mark.parametrize("path", PATHS)
def test_candidate_free_discovery_is_valid(path):
    snap = _snap({"hyp-subj-a": _candidate(1, ["n1"], ["P1"])})
    questions = _run(path, snapshot=snap, questions=[
        _question("q1", ["n2"], refs=[], principals=["P3"]),
    ])
    assert questions[0].hypothesis_revisions == {}


@pytest.mark.parametrize("path", PATHS)
def test_revision_mismatch_is_reported_before_subject_alignment(path):
    snap = _snap({"hyp-subj-a": _candidate(4, ["n1"])})
    updates = [_update("hyp-subj-a", expected=3, targets=("n2",))]
    with pytest.raises(AtomizationProposalError) as caught:
        _run(path, snapshot=snap, updates=updates,
             questions=[_question("q1", ["n1"], refs=["hyp-subj-a"])])
    assert caught.value.code == "candidate_expected_revision_not_in_snapshot"

