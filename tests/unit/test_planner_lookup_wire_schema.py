"""Planner lookup JSON Schema matches its Pydantic query-shape rules."""

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from redteam.orchestration.hypothesis_planner import (
    HypothesisDecision,
    _split_plan_response_schema,
)
from redteam.orchestration.planner_context import PlannerContextRequest
from redteam.orchestration.probe_atomization import HypothesisPlanSelection

CONTEXT_QUERY = {"kind": "task", "mode": "list"}
EVIDENCE_QUERY = {"mode": "list"}

VALID_LOOKUPS = [
    {"context": CONTEXT_QUERY},
    {"version": 1, "context": None, "evidence": EVIDENCE_QUERY, "queries": None},
    {
        "version": 2,
        "context": None,
        "evidence": None,
        "queries": [
            {"context": CONTEXT_QUERY},
            {"evidence": EVIDENCE_QUERY},
            {"context": CONTEXT_QUERY, "evidence": None},
            {"context": None, "evidence": EVIDENCE_QUERY},
        ],
    },
]

INVALID_LOOKUPS = [
    {},
    {"version": 1},
    {"version": 1, "context": CONTEXT_QUERY, "evidence": EVIDENCE_QUERY},
    {"version": 1, "context": CONTEXT_QUERY, "queries": []},
    {"version": 2},
    {"version": 2, "queries": []},
    {"version": 2, "queries": [{}]},
    {"version": 2, "queries": [{"context": None, "evidence": None}]},
    {"version": 2, "queries": [{"context": CONTEXT_QUERY, "evidence": EVIDENCE_QUERY}]},
    {"version": 2, "context": CONTEXT_QUERY, "queries": [{"context": CONTEXT_QUERY}]},
    {"version": 2, "queries": [
        {"context": CONTEXT_QUERY}, {"context": CONTEXT_QUERY},
        {"context": CONTEXT_QUERY}, {"context": CONTEXT_QUERY},
        {"context": CONTEXT_QUERY},
    ]},
    {"version": 3, "context": CONTEXT_QUERY},
]


@pytest.fixture(scope="module")
def lookup_validator():
    schema = PlannerContextRequest.model_json_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("lookup", VALID_LOOKUPS)
def test_json_schema_and_pydantic_accept_the_same_lookup_shapes(lookup, lookup_validator):
    assert lookup_validator.is_valid(lookup)
    PlannerContextRequest.model_validate(lookup)


@pytest.mark.parametrize("lookup", INVALID_LOOKUPS)
def test_json_schema_and_pydantic_reject_empty_or_mixed_lookup_shapes(
    lookup, lookup_validator,
):
    assert not lookup_validator.is_valid(lookup)
    with pytest.raises(ValidationError):
        PlannerContextRequest.model_validate(lookup)


def test_nullable_embedded_lookup_is_preserved_and_guided_in_schema():
    schema = HypothesisDecision.model_json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    request_description = schema["$defs"]["PlannerContextRequest"]["description"]

    assert "set it to null when no lookup is needed" in request_description
    for payload in (
        {"reason": "No lookup is needed", "context_request": None},
        {"reason": "Use the legacy v1 lookup", "context_request": VALID_LOOKUPS[0]},
        {"reason": "Use a bounded batch", "context_request": VALID_LOOKUPS[2]},
    ):
        assert validator.is_valid(payload)
        HypothesisDecision.model_validate(payload)

    invalid = {"reason": "Empty lookup", "context_request": {"version": 1}}
    assert not validator.is_valid(invalid)
    with pytest.raises(ValidationError):
        HypothesisDecision.model_validate(invalid)

    split_schema = _split_plan_response_schema(HypothesisPlanSelection.model_json_schema())
    Draft202012Validator.check_schema(split_schema)
    split_validator = Draft202012Validator(split_schema)
    split_valid = {
        "reason": "No lookup is needed",
        "questions": [],
        "termination": "held",
        "context_request": None,
    }
    assert split_validator.is_valid(split_valid)
    HypothesisPlanSelection.model_validate(split_valid)

    split_invalid = {
        **split_valid,
        "context_request": {"version": 1},
    }
    assert not split_validator.is_valid(split_invalid)
    with pytest.raises(ValidationError):
        HypothesisPlanSelection.model_validate(split_invalid)
