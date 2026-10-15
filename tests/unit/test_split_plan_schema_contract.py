"""Stage-1 split replies state questions and termination; explicit empty lists stay valid."""

import jsonschema

from redteam.core.contracts.completion import CompletionCriterion
from redteam.orchestration.hypothesis_planner import _split_plan_response_schema
from redteam.orchestration.planner_context import lookup_only
from redteam.orchestration.probe_atomization import HypothesisPlanDraft
from tests.integration.test_v3_atomization import _plan_two_questions
from tests.unit.test_split_planner_repair_guidance import _advance_with

STAGE_ONE_SCHEMA = _split_plan_response_schema(HypothesisPlanDraft.model_json_schema())


def _omitted_questions():
    return {
        "updates": [],
        "reason": "Await an observation",
        "termination": "held",
        "resume_conditions": ["An observation arrives"],
    }


def _explicit_held():
    return {
        "updates": [],
        "questions": [],
        "reason": "Await an observation",
        "termination": "held",
        "resume_conditions": ["An observation arrives"],
    }


def test_schema_requires_questions_and_termination_but_not_updates():
    assert {"reason", "questions", "termination"} <= set(STAGE_ONE_SCHEMA["required"])
    assert "updates" not in STAGE_ONE_SCHEMA["required"]
    assert "lookup" in STAGE_ONE_SCHEMA["properties"]["termination"]["description"]


def test_persisted_draft_defaults_are_unchanged():
    draft = HypothesisPlanDraft.model_validate({"reason": "Keep watching"})
    assert draft.questions == []
    assert draft.termination == "continue"


def test_explicit_empty_context_lookup_is_schema_valid_and_lookup_only():
    lookup = {
        "updates": [],
        "questions": [],
        "reason": "Read the Task",
        "termination": "continue",
        "context_request": {"version": 1, "context": {"kind": "task", "mode": "list"}},
    }
    jsonschema.validate(instance=lookup, schema=STAGE_ONE_SCHEMA)
    assert lookup_only(HypothesisPlanDraft.model_validate(lookup)) is True


async def test_omitted_questions_fail_schema_then_valid_plan_commits(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path,
        [_omitted_questions(), {**_plan_two_questions(), "termination": "continue"}],
    )
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    assert "schema_required at $.questions" in llm.plan_feedback[1]


async def test_omitted_termination_fails_schema_then_valid_plan_commits(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path,
        [
            {key: value for key, value in _plan_two_questions().items() if key != "termination"},
            _plan_two_questions(),
        ],
    )
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    assert "schema_required at $.termination" in llm.plan_feedback[1]


def test_criterion_kind_and_description_are_required_only_in_model_facing_schema():
    criterion = STAGE_ONE_SCHEMA["$defs"]["CompletionCriterion"]
    assert {"kind", "description"} <= set(criterion["required"])
    shared = HypothesisPlanDraft.model_json_schema()["$defs"]["CompletionCriterion"]
    assert "kind" not in shared.get("required", [])
    assert CompletionCriterion().kind == "unknown"
    jsonschema.validate(instance={"kind": "observation", "description": ""}, schema=criterion)


def _criterion_without(field):
    plan = _plan_two_questions()
    plan["questions"][0]["completion_criteria"][0].pop(field)
    return plan


async def test_omitted_criterion_kind_is_schema_rejected_then_repaired_and_atomized(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path,
        [_criterion_without("kind"), _plan_two_questions()],
    )
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    assert "schema_required at $.questions[0].completion_criteria[0].kind" in llm.plan_feedback[1]


async def test_omitted_criterion_description_is_schema_rejected_then_repaired(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path,
        [_criterion_without("description"), _plan_two_questions()],
    )
    assert len(ids) == 2 and spent == 3
    assert len(llm.atom_inputs) == 1
    assert (
        "schema_required at $.questions[0].completion_criteria[0].description"
        in llm.plan_feedback[1]
    )


async def test_explicit_held_with_empty_questions_is_accepted_without_atomizer(tmp_path):
    ids, spent, llm = await _advance_with(tmp_path, [_explicit_held()])
    assert ids == []
    assert spent == 1
    assert llm.atom_inputs == []
