"""planning/actions.py 단위 테스트 — 액션 입력 스키마 재검증."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finredteam.planning.actions import (
    DiscoverSurfaceInput,
    ExecuteExperimentInput,
    FinishInput,
    LearnBaselineInput,
    ObserveGraphInput,
    ProposeHypothesisInput,
    ReActStep,
    RecallMemoryInput,
    parse_action_input,
)


def test_parse_observe_graph_empty_input_ok():
    result = parse_action_input("observe_graph", {})
    assert isinstance(result, ObserveGraphInput)


def test_parse_propose_hypothesis_valid():
    result = parse_action_input(
        "propose_hypothesis",
        {"operation": "transfer", "principal_id": "attacker_A", "target": "account:1", "params": {"amount": "-5"}},
    )
    assert isinstance(result, ProposeHypothesisInput)
    assert result.operation == "transfer"
    assert result.params == {"amount": "-5"}


def test_parse_propose_hypothesis_missing_required_field_returns_error_string():
    result = parse_action_input("propose_hypothesis", {"target": "account:1"})
    assert isinstance(result, str)
    assert "입력 형식 오류" in result


def test_parse_execute_experiment_requires_hypothesis_id():
    ok = parse_action_input("execute_experiment", {"hypothesis_id": "hyp_1"})
    assert isinstance(ok, ExecuteExperimentInput)
    bad = parse_action_input("execute_experiment", {})
    assert isinstance(bad, str)


def test_parse_discover_surface_defaults_method_to_get():
    result = parse_action_input("discover_surface", {"path": "/api/v2/whatever"})
    assert isinstance(result, DiscoverSurfaceInput)
    assert result.method == "GET"


def test_parse_discover_surface_rejects_mutating_method():
    result = parse_action_input("discover_surface", {"path": "/x", "method": "POST"})
    assert isinstance(result, str)


def test_parse_learn_baseline_requires_op():
    ok = parse_action_input("learn_baseline", {"op": "transfer"})
    assert isinstance(ok, LearnBaselineInput)
    bad = parse_action_input("learn_baseline", {})
    assert isinstance(bad, str)


def test_parse_recall_memory_defaults_query_to_empty():
    result = parse_action_input("recall_memory", {})
    assert isinstance(result, RecallMemoryInput)
    assert result.query == ""


def test_parse_finish_defaults_reason_to_empty():
    result = parse_action_input("finish", {})
    assert isinstance(result, FinishInput)
    assert result.reason == ""


def test_parse_unknown_action_returns_error_string():
    result = parse_action_input("delete_everything", {})
    assert isinstance(result, str)
    assert "알 수 없는 액션" in result


def test_react_step_rejects_invalid_action_literal():
    with pytest.raises(ValidationError):
        ReActStep.model_validate({"thought": "x", "action": "delete_everything", "action_input": {}})


def test_react_step_accepts_valid_action():
    step = ReActStep.model_validate({"thought": "x", "action": "finish", "action_input": {"reason": "done"}})
    assert step.action == "finish"
