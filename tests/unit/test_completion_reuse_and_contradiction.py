"""plan7d: contradictory AND verdicts are rejected, legitimate source reuse is not.

Covers the fixed bug from the plan7 base run (`run_a3947f4a3758`): two probe
tasks each required both `hypothesis_confirmed` and `hypothesis_refuted` for
the same candidate -- a condition that can never be satisfied, since a
Hypothesis has one status at a time -- and a legitimate follow-up citing its
declared source task's evidence was rejected as "purpose changed since the
evidence". Both are exercised through the real validators/registration path
(temporary SQLite, synthetic originals, no model/provider execution), plus the
two small pure helpers directly.
"""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.completion import CompletionCriterion, contradictory_candidate_verdict
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    bind_plan,
)
from redteam.orchestration.semantic_completion import _cites_declared_source
from redteam.orchestration.specialist_tasks import SpecialistProposalError
from tests.unit.test_hypothesis_probes import admit, candidate, probe, setup
from tests.unit.test_probe_atomization import PRINCIPALS, TARGETS, _question, _snap, _update


def _criteria(*kinds_and_hids):
    return [
        CompletionCriterion(kind=kind, description=f"check {hid}", hypothesis_id=hid)
        for kind, hid in kinds_and_hids
    ]


# -- the pure contract-level check -------------------------------------------


def test_contradiction_is_only_the_same_hypothesis_id_confirmed_and_refuted():
    assert contradictory_candidate_verdict(
        _criteria(("hypothesis_confirmed", "h1"), ("hypothesis_refuted", "h1"))
    ) == "h1"


@pytest.mark.parametrize("criteria", [
    _criteria(("hypothesis_confirmed", "h1"), ("hypothesis_refuted", "h2")),  # different candidates
    _criteria(("hypothesis_confirmed", "h1"), ("hypothesis_confirmed", "h1")),  # same kind twice
    _criteria(("hypothesis_confirmed", "h1")),  # single verdict is never contradictory
    [CompletionCriterion(kind="observation", description="plain check")],  # no candidate
])
def test_normal_combinations_are_never_flagged(criteria):
    assert contradictory_candidate_verdict(criteria) is None


# -- stage 1 (before the atomizer is ever called) ----------------------------


def test_bind_plan_rejects_a_contradictory_question_before_atomization():
    draft = {
        "updates": [_update("candidate-a", expected=None)],
        "reason": "check both directions",
        "questions": [_question(
            criteria=[{"kind": "hypothesis_confirmed", "description": "confirms it",
                      "hypothesis_id": "candidate-a"},
                     {"kind": "hypothesis_refuted", "description": "refutes it",
                      "hypothesis_id": "candidate-a"}],
            revisions={"candidate-a": 1},
        )],
    }
    issued = {}
    with pytest.raises(AtomizationProposalError) as excinfo:
        bind_plan(
            HypothesisPlanDraft.model_validate(draft), _snap(), issued=issued,
            targets=set(TARGETS), principals=set(PRINCIPALS), max_tasks=8,
        )
    assert excinfo.value.code == "same_candidate_completion_condition_conflict"
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.trace[0]["hypothesis_id"] == issued["candidate-a"]
    assert len(excinfo.value.diagnostic.criterion_ids) == 2
    assert excinfo.value.diagnostic.retryable is True


def test_bind_plan_accepts_two_questions_each_with_one_verdict_for_the_same_candidate():
    draft = {
        "updates": [_update("candidate-a", expected=None)],
        "reason": "two independent purposes",
        "questions": [
            _question(qid="q1", criteria=[{"kind": "hypothesis_confirmed",
                                           "description": "confirms it",
                                           "hypothesis_id": "candidate-a"}],
                      revisions={"candidate-a": 1}),
            _question(qid="q2", criteria=[{"kind": "hypothesis_refuted",
                                           "description": "refutes a different angle",
                                           "hypothesis_id": "candidate-a"}],
                      revisions={"candidate-a": 1}),
        ],
    }
    updates, questions, versions = bind_plan(
        HypothesisPlanDraft.model_validate(draft), _snap(), issued={},
        targets=set(TARGETS), principals=set(PRINCIPALS), max_tasks=8,
    )
    assert [q.question_id for q in questions] == ["q1", "q2"]


# -- the canonical registration boundary (register_probe_tasks) -------------


async def test_registration_rejects_a_probe_task_with_both_verdicts_for_one_candidate(tmp_path):
    deps, service = await setup(tmp_path)
    await admit(deps, service, "seed", updates=[candidate(revision=None, status="open")])
    contradictory = probe(
        completion_criteria=[
            {"kind": "hypothesis_confirmed", "description": "confirms it",
             "hypothesis_id": "candidate-a"},
            {"kind": "hypothesis_refuted", "description": "refutes it",
             "hypothesis_id": "candidate-a"},
        ],
    )
    with pytest.raises(SpecialistProposalError) as excinfo:
        await admit(deps, service, "contradictory", tasks=[contradictory])
    assert excinfo.value.code == "same_candidate_completion_condition_conflict"
    assert excinfo.value.diagnostic.trace[0]["hypothesis_id"] == "candidate-a"
    assert len(excinfo.value.diagnostic.criterion_ids) == 2


async def test_registration_allows_two_separate_tasks_each_with_one_verdict(tmp_path):
    deps, service = await setup(tmp_path)
    await admit(deps, service, "seed", updates=[candidate(revision=None, status="open")])
    confirms = probe(key="probe-confirm", completion_criteria=[
        {"kind": "hypothesis_confirmed", "description": "confirms it",
         "hypothesis_id": "candidate-a"},
    ])
    refutes = probe(key="probe-refute", completion_criteria=[
        {"kind": "hypothesis_refuted", "description": "refutes a different angle",
         "hypothesis_id": "candidate-a"},
    ])
    receipt = await admit(deps, service, "split", tasks=[confirms, refutes])
    assert len(receipt["task_ids"]) == 2


# -- legitimate source-task evidence reuse (semantic_completion) ------------


def test_a_task_with_no_declared_source_gets_no_exception():
    task = SimpleNamespace(source_task_id=None)
    assert _cites_declared_source(task, {"mission_task_id": "task_a"}) is False


def test_citing_the_declared_source_tasks_own_evidence_is_recognized():
    task = SimpleNamespace(source_task_id="task_a")
    assert _cites_declared_source(task, {"mission_task_id": "task_a"}) is True


def test_citing_an_unrelated_tasks_evidence_is_still_rejected():
    # Declaring source_task_id does not make every other task's evidence related.
    task = SimpleNamespace(source_task_id="task_a")
    assert _cites_declared_source(task, {"mission_task_id": "task_b"}) is False
    assert _cites_declared_source(task, {}) is False
