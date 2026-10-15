"""Legacy specialist planner rejects name the StrategyDraft rule that failed.
The repair request and the exhausted diagnostic must carry the planner's own rule code and
its field location. Model text (the reason, resume conditions) stays out of diagnostics.
"""

import json

import pytest
from pydantic import ValidationError

from redteam.app.central_development import build_central_development
from redteam.app.central_run import bind_run_metadata
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.specialist_planner import (
    SpecialistPlanner,
    StrategyDraft,
    _strategy_rule_error,
)
from tests.integration.test_central_development import engagement, gateway, settings

REASON_MARKER = "RAW-PLANNER-REASON-7f3a"
RESUME_MARKER = "RAW-PLANNER-RESUME-2b9e"


def _held_without_resume():
    return {
        "questions": [],
        "reason": REASON_MARKER,
        "termination": "held",
        "resume_conditions": [],
    }


def _blank_reason():
    return {
        "questions": [],
        "reason": "   ",
        "termination": "held",
        "resume_conditions": [RESUME_MARKER],
    }


def _valid_held():
    return {
        "questions": [],
        "reason": "Waiting for an operator-supplied scope",
        "termination": "held",
        "resume_conditions": ["Operator supplies the in-scope target"],
    }


def _rejected(failure: DecisionFailure):
    return failure.diagnostic.last_cause or failure.diagnostic


def _strategy_error(payload) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        StrategyDraft.model_validate(payload)
    return caught.value


def test_missing_resume_condition_keeps_its_rule_code():
    error = _strategy_rule_error(_strategy_error(_held_without_resume()))
    assert error.code == "planner_resume_condition_required"


def test_blank_reason_keeps_its_rule_code():
    error = _strategy_rule_error(_strategy_error(_blank_reason()))
    assert error.code == "planner_reason_blank"


def test_unknown_validation_error_keeps_the_generic_code():
    unknown = ValueError("planner_is_not_a_known_rule")
    assert _strategy_rule_error(unknown).code == "specialist_invalid_proposal"


def test_rule_rejection_names_its_field_and_repair_summary():
    error = _strategy_rule_error(_strategy_error(_held_without_resume()))
    diagnostic = SpecialistPlanner._rejection(error, "attack_path_proposal").diagnostic
    assert diagnostic.code == "planner_resume_condition_required"
    assert diagnostic.location == "$.resume_conditions"
    assert diagnostic.validation_issues == [{
        "rule": "planner_resume_condition_required",
        "location": "$.resume_conditions",
    }]
    assert "resume_conditions" in diagnostic.summary
    assert "failed the captured subject" not in diagnostic.summary


async def test_repair_request_names_the_rule_after_an_invalid_judgment():
    llm = FakeLLMClient(structured_responses=[_held_without_resume(), _valid_held()])
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await bind_run_metadata(selection.deps)
        assert await SpecialistPlanner(selection.deps).advance(selection.runtime) == []

    assert len(llm.requests) == 2
    first, repair = (json.dumps(request["messages"]) for request in llm.requests)
    assert "planner_resume_condition_required" not in first
    assert "planner_resume_condition_required" in repair
    assert "$.resume_conditions" in repair
    assert "specialist_invalid_proposal" not in repair
    assert REASON_MARKER not in repair


async def test_exhausted_missing_resume_diagnostic_masks_model_text():
    llm = FakeLLMClient(canned_structured=_held_without_resume())
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await bind_run_metadata(selection.deps)
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(selection.deps).advance(selection.runtime)

    diagnostic = _rejected(failed.value)
    assert diagnostic.code == "planner_resume_condition_required"
    assert diagnostic.location == "$.resume_conditions"
    text = diagnostic.model_dump_json() + str(failed.value)
    assert REASON_MARKER not in text


async def test_exhausted_blank_reason_diagnostic_masks_model_text():
    llm = FakeLLMClient(canned_structured=_blank_reason())
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await bind_run_metadata(selection.deps)
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(selection.deps).advance(selection.runtime)

    diagnostic = _rejected(failed.value)
    assert diagnostic.code == "planner_reason_blank"
    assert diagnostic.location == "$.reason"
    text = diagnostic.model_dump_json() + str(failed.value)
    assert RESUME_MARKER not in text

