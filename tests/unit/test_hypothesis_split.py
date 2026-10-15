"""Split-flow helpers: atomizer strategy input + atomizer skip predicates."""

from types import SimpleNamespace

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.specialists import ProbeLimits
from redteam.orchestration.probe_atomization import (
    HypothesisPlanDraft,
    TaskAtomizationDraft,
    bind_plan,
    build_atomizer_input,
    needs_atomization,
    resolve_atomized_tasks,
    validate_atomized_tasks,
)


def _cand(identity="cand-a", revision=1):
    return Hypothesis(
        contract_version=2, hypothesis_id=identity, description="Access may be unguarded",
        target_node_ids=["host:a"], reason="Verify the observed response",
        status="open", revision=revision,
    )


def _snap(*candidates):
    return SimpleNamespace(
        candidates={item.hypothesis_id: item for item in candidates}, tasks={},
    )


def _question(qid="q1", **over):
    body = {
        "question_id": qid,
        "objective": "Inspect one response",
        "target_node_ids": ["host:a"],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
    }
    body.update(over)
    return body


def _new_update(key="local"):
    return {"expected_revision": None, "hypothesis": {
        "contract_version": 2, "hypothesis_id": key,
        "description": "Access may be unguarded", "target_node_ids": ["host:a"],
        "reason": "Verify the observed response", "status": "open",
    }}


def _existing_update():
    return {"expected_revision": 1, "hypothesis": {
        "contract_version": 2, "hypothesis_id": "cand-a",
        "description": "Access may be unguarded", "target_node_ids": ["host:a"],
        "reason": "Verify the observed response", "status": "open", "revision": 1,
    }}


def test_atomizer_input_carries_full_public_strategy():
    snapshot = _snap()  # stale for the same-pass candidate: nothing captured
    body = {
        "updates": [_new_update()],
        "questions": [_question("q1", hypothesis_revisions={"local": 1})],
        "reason": "Follow the new lead",
    }
    draft = HypothesisPlanDraft.model_validate(body)
    issued = {}
    updates, questions, versions = bind_plan(
        draft, snapshot, issued=issued, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    atom_in = build_atomizer_input(
        {"candidates": [], "probe_limits": {"max_actions": 4, "max_model_requests": 8}},
        plan=draft, updates=updates, questions=questions, issued=issued, versions=versions,
    )
    strategy = atom_in["strategy"]
    assert strategy["reason"] == "Follow the new lead"
    assert strategy["termination"] == "continue"
    (normalized,) = strategy["updates"]
    assert normalized["hypothesis"]["hypothesis_id"] == issued["local"]
    assert normalized["hypothesis"]["description"] == "Access may be unguarded"
    assert normalized["expected_revision"] is None
    assert atom_in["issued_candidate_ids"] == issued
    assert atom_in["candidate_revisions"] == {issued["local"]: 1}
    assert [item["question_id"] for item in atom_in["questions"]] == ["q1"]
    assert issued["local"] not in {item["hypothesis_id"] for item in atom_in["candidates"]}


def test_held_plan_skips_atomizer():
    snapshot = _snap(_cand())
    draft = HypothesisPlanDraft.model_validate({
        "reason": "Await prerequisites", "termination": "held",
        "resume_conditions": ["An authorized prerequisite arrives"],
    })
    updates, questions, versions = bind_plan(
        draft, snapshot, issued={}, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    assert updates is None and questions == []
    assert not needs_atomization(draft, questions)


def test_update_only_plan_skips_atomizer():
    snapshot = _snap(_cand())
    draft = HypothesisPlanDraft.model_validate({
        "updates": [_existing_update()], "termination": "replan",
        "reason": "Review one captured candidate",
    })
    updates, questions, versions = bind_plan(
        draft, snapshot, issued={}, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    assert updates is not None and questions == []
    assert not needs_atomization(draft, questions)


def test_questioned_plan_needs_atomizer():
    snapshot = _snap(_cand())
    draft = HypothesisPlanDraft.model_validate({
        "questions": [_question()], "reason": "Short plan",
    })
    updates, questions, versions = bind_plan(
        draft, snapshot, issued={}, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    assert updates == [] and len(questions) == 1
    assert needs_atomization(draft, questions)


def test_strategy_update_marks_proposed_revision():
    snapshot = _snap(_cand())
    body = {
        "updates": [_existing_update()],
        "questions": [_question("q1", hypothesis_revisions={"cand-a": 2})],
        "reason": "Re-probe the candidate",
    }
    draft = HypothesisPlanDraft.model_validate(body)
    issued = {}
    updates, questions, versions = bind_plan(
        draft, snapshot, issued=issued, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    assert versions == {"cand-a": 2}
    atom_in = build_atomizer_input(
        {"candidates": [{"hypothesis_id": "cand-a", "revision": 1}]},
        plan=draft, updates=updates, questions=questions, issued=issued, versions=versions,
    )
    (entry,) = atom_in["strategy"]["updates"]
    assert entry["hypothesis"]["revision"] == 1
    assert entry["proposed_revision"] == 2
    assert atom_in["candidate_revisions"] == {"cand-a": 2}
    assert questions[0].hypothesis_revisions == {"cand-a": 2}


def test_existing_criterion_hypothesis_id_survives_normalization():
    snapshot = _snap(_cand())
    criterion = {"kind": "observation", "description": "Inspect response",
                 "hypothesis_id": "cand-a"}
    expected_id = criterion_id(CompletionCriterion.model_validate(criterion))
    body = {
        "updates": [_existing_update()],
        "questions": [_question("q1", hypothesis_revisions={"cand-a": 2},
                                    completion_criteria=[criterion])],
        "reason": "Re-probe the candidate",
    }
    draft = HypothesisPlanDraft.model_validate(body)
    issued = {}
    updates, questions, versions = bind_plan(
        draft, snapshot, issued=issued, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    assert versions == {"cand-a": 2}
    (bound_criterion,) = questions[0].completion_criteria
    assert bound_criterion.hypothesis_id == "cand-a"
    assert bound_criterion.description == "Inspect response"
    assert criterion_id(bound_criterion) == expected_id
    parsed = TaskAtomizationDraft.model_validate({"tasks": [{
        "task_key": "t1", "question_id": "q1", "objective": "Inspect one response",
        "title": "Response check", "role_id": "probe-role",
        "target_node_ids": ["host:a"], "completion_criterion_ids": [expected_id],
        "hypothesis_revisions": {"cand-a": 2},
    }]})
    (stripped,) = validate_atomized_tasks(
        resolve_atomized_tasks(parsed.tasks, questions=questions),
        questions=questions, issued=issued, versions=versions,
        targets={"host:a"}, principals=set(), roles={"probe-role"},
        limits=ProbeLimits(), snapshot=snapshot, max_tasks=4,
    )
    (carried,) = stripped.completion_criteria
    assert carried.hypothesis_id == "cand-a"
    assert carried.description == "Inspect response"
    assert criterion_id(carried) == expected_id


def test_atomizer_explicit_handoff_omits_unrelated_candidates_and_budget_history():
    snapshot = _snap(_cand(), _cand("unrelated"))
    draft = HypothesisPlanDraft.model_validate({"questions": [
        _question(hypothesis_revisions={"cand-a": 1})], "reason": "One related purpose"})
    updates, questions, versions = bind_plan(
        draft, snapshot, issued={}, targets={"host:a"}, principals=set(), max_tasks=4,
    )
    output = build_atomizer_input({
        "candidates": [item.model_dump(mode="json") for item in snapshot.candidates.values()],
        "model_usage": {"history": ["unrelated"] * 100},
        "review_receipts": [{"metadata": {"private_reasoning": "Do not forward"}}],
        "arbitrary_planner_field": "Must not leak into the handoff",
    }, plan=draft, updates=updates, questions=questions, issued={}, versions=versions)
    assert versions == {"cand-a": 1, "unrelated": 1}  # Full validation authority is intact.
    assert output["candidate_revisions"] == {"cand-a": 1}
    assert [item["hypothesis_id"] for item in output["candidates"]] == ["cand-a"]
    assert "model_usage" not in output and "review_receipts" not in output
    assert "arbitrary_planner_field" not in output
