"""Structured diagnostics from the real legacy and split hypothesis-planning loops."""

from copy import deepcopy

import pytest

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import PROFILE
from tests.integration.test_probe_workers import selection as probe_selection


class PlannerResponseLLM(FakeLLMClient):
    """Return fixed structured proposals while exercising the production validators."""

    def __init__(self, *, legacy=None, plan=None, atom=None):
        super().__init__()
        self.responses = {"legacy": legacy, "plan": plan, "atom": atom}
        self.calls = {key: 0 for key in self.responses}

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema.get("properties", {})
        if "questions" in properties:
            key = "plan"
        elif "tasks" in properties and "updates" not in properties:
            key = "atom"
        else:
            key = "legacy"
        await self._wait_response("complete_structured", messages)
        self.calls[key] += 1
        return deepcopy(self.responses[key])


def _rejected(failure: DecisionFailure):
    """Return the callback's final rejection from the exhausted repair diagnostic."""
    return failure.diagnostic.last_cause or failure.diagnostic


def _assert_private_values_absent(failure: DecisionFailure, *values: str) -> None:
    diagnostic_text = failure.diagnostic.model_dump_json()
    for value in values:
        assert value not in diagnostic_text
        assert value not in str(failure)


def _exercise_owner_validation(monkeypatch):
    """Let the planner callback own these errors after the normal JSON Schema pass.

    StructuredDecisionClient normally rejects malformed JSON Schema responses
    before calling validate_response. This fixture-only seam keeps the real
    planner loop, structured client and callback, while isolating the callback's
    Pydantic diagnostic behavior.
    """
    from redteam.orchestration import cognition

    monkeypatch.setattr(cognition, "_validate_schema", lambda **kwargs: None)


def _valid_plan():
    return {
        "updates": [],
        "questions": [{
            "question_id": "q1",
            "objective": "Inspect the captured response",
            "target_node_ids": [TARGET],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the preserved response",
            }],
        }],
        "reason": "One scoped observation is useful",
    }


async def test_split_plan_validation_keeps_declared_and_nested_locations(tmp_path, monkeypatch):
    _exercise_owner_validation(monkeypatch)
    extra_marker = "RAW-PLAN-EXTRA-9c16"
    objective_marker = "RAW-PLAN-OBJECTIVE-0c2a"
    nested_value_marker = "RAW-PLAN-TARGETS-4e71"
    response = {
        "updates": [],
        "questions": [{
            "question_id": "q1",
            "objective": objective_marker,
            "target_node_ids": nested_value_marker,
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the preserved response",
            }],
        }],
        "unsupported_private_field": extra_marker,
    }
    llm = PlannerResponseLLM(plan=response)
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

    diagnostic = _rejected(failed.value)
    locations = {issue["location"] for issue in diagnostic.validation_issues}
    rules = {issue["rule"] for issue in diagnostic.validation_issues}
    assert diagnostic.stage == "hypothesis_planning"
    assert diagnostic.code == "hypothesis_proposal_invalid"
    assert diagnostic.kind == "validation" and diagnostic.retryable is True
    assert diagnostic.summary.startswith("Use captured subjects, candidate revisions")
    assert diagnostic.error_type == "ValidationError"
    assert diagnostic.exception_chain
    assert {"$.reason", "$.questions[0].target_node_ids"} <= locations
    assert "missing" in rules and "list_type" in rules and "extra_forbidden" in rules
    _assert_private_values_absent(failed.value, objective_marker, nested_value_marker, extra_marker)


async def test_split_outer_schema_gate_keeps_declared_field_location(tmp_path):
    marker = "RAW-OUTER-PLAN-OBJECTIVE-812d"
    response = {
        "updates": [],
        "questions": [{
            "question_id": "q1",
            "objective": marker,
            "target_node_ids": [TARGET],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the preserved response",
            }],
        }],
        # The missing required reason sends this through the normal outer JSON Schema gate.
    }
    llm = PlannerResponseLLM(plan=response)
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

    diagnostic = _rejected(failed.value)
    assert diagnostic.code.startswith("schema_")
    assert diagnostic.validation_issues
    assert {issue["location"] for issue in diagnostic.validation_issues} == {"$.reason"}
    _assert_private_values_absent(failed.value, marker)


async def test_task_atomization_validation_keeps_stage_summary_and_safe_issues(
    tmp_path, monkeypatch,
):
    _exercise_owner_validation(monkeypatch)
    task_marker = "RAW-ATOM-TASKSET-a3f8"
    llm = PlannerResponseLLM(plan=_valid_plan(), atom={"tasks": [], "private": task_marker})
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

    diagnostic = _rejected(failed.value)
    assert diagnostic.stage == "task_atomization"
    assert diagnostic.code == "hypothesis_proposal_invalid"
    assert diagnostic.kind == "validation" and diagnostic.retryable is True
    assert diagnostic.summary.startswith("Cover every planned question")
    assert diagnostic.error_type == "ValidationError"
    assert diagnostic.exception_chain
    assert diagnostic.validation_issues
    assert {issue["location"] for issue in diagnostic.validation_issues} >= {"$.tasks", "$"}
    assert {issue["rule"] for issue in diagnostic.validation_issues} >= {
        "too_short", "extra_forbidden",
    }
    _assert_private_values_absent(failed.value, task_marker)


async def test_legacy_decision_validation_keeps_declared_nested_locations(tmp_path, monkeypatch):
    _exercise_owner_validation(monkeypatch)
    objective_marker = "RAW-LEGACY-OBJECTIVE-7b52"
    nested_value_marker = "RAW-LEGACY-TARGETS-0391"
    extra_marker = "RAW-LEGACY-EXTRA-3f20"
    response = {
        "updates": [],
        "tasks": [{
            "task_key": "observe",
            "objective": objective_marker,
            "title": "Observe one response",
            "role_id": "probe-worker",
            "target_node_ids": nested_value_marker,
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the preserved response",
            }],
        }],
        "unsupported_private_field": extra_marker,
    }
    llm = PlannerResponseLLM(legacy=response)
    legacy_profile = PROFILE.with_name("native-probes-legacy.yaml")
    async with probe_selection(tmp_path, profile=legacy_profile, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

    diagnostic = _rejected(failed.value)
    locations = {issue["location"] for issue in diagnostic.validation_issues}
    rules = {issue["rule"] for issue in diagnostic.validation_issues}
    assert diagnostic.stage == "hypothesis_planning"
    assert diagnostic.code == "hypothesis_proposal_invalid"
    assert diagnostic.kind == "validation" and diagnostic.retryable is True
    assert diagnostic.summary.startswith("Use captured subjects, ready roles")
    assert diagnostic.error_type == "ValidationError"
    assert diagnostic.exception_chain
    assert {"$.reason", "$.tasks[0].target_node_ids"} <= locations
    assert "missing" in rules and "list_type" in rules and "extra_forbidden" in rules
    _assert_private_values_absent(failed.value, objective_marker, nested_value_marker, extra_marker)


async def test_legacy_coded_semantic_error_preserves_proposal_code(tmp_path):
    reason_marker = "RAW-LEGACY-REASON-2bd9"
    llm = PlannerResponseLLM(legacy={
        "updates": [], "tasks": [], "reason": reason_marker,
    })
    legacy_profile = PROFILE.with_name("native-probes-legacy.yaml")
    async with probe_selection(tmp_path, profile=legacy_profile, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

    diagnostic = _rejected(failed.value)
    assert diagnostic.code == "empty_judgment_needs_termination_and_resume_conditions"
    assert diagnostic.code not in {"model_validation", "invalid_decision"}
    assert diagnostic.error_type == "HypothesisProposalError"
    assert diagnostic.kind == "validation" and diagnostic.retryable is True
    _assert_private_values_absent(failed.value, reason_marker)


async def test_atomization_coded_semantic_error_preserves_proposal_code(tmp_path):
    question_marker = "RAW-ATOM-QUESTION-4f8a"
    atom = {"tasks": [{
        "task_key": "probe-one",
        "question_id": question_marker,
        "objective": "Inspect the captured response",
        "title": "Response observation",
        "role_id": "probe-worker",
        "target_node_ids": [TARGET],
        "completion_criterion_ids": [criterion_id(CompletionCriterion(
            kind="observation", description="Inspect the preserved response",
        ))],
    }]}
    llm = PlannerResponseLLM(plan=_valid_plan(), atom=atom)
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

    diagnostic = _rejected(failed.value)
    assert diagnostic.stage == "task_atomization"
    assert diagnostic.code == "atomized_unknown_question"
    assert diagnostic.code not in {"model_validation", "invalid_decision"}
    assert diagnostic.error_type == "AtomizationProposalError"
    assert diagnostic.kind == "validation" and diagnostic.retryable is True
    _assert_private_values_absent(failed.value, question_marker)
