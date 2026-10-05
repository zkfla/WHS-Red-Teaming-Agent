"""Generated criteria stay strict while stored legacy conditions remain readable."""

from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import BaseModel

from redteam.orchestration.engine import ActionIntent
from redteam.orchestration.paths.models import (
    ALLOWED_CRITERION_KINDS,
    CompletionCriterion,
    PathPlan,
    PathStep,
)
from redteam.orchestration.paths.writing import (
    CriterionWritingError,
    validate_written_criteria,
    writing_schema,
)


def _criterion(kind: str = "evidence") -> dict:
    return {
        "kind": kind,
        "description": "Collect evidence of the authorized observation.",
        "min_evidence": 1,
        "hypothesis_id": "hypothesis-1",
    }


def _path(criterion: object) -> dict:
    return {
        "path_id": "path-1",
        "run_id": "run-1",
        "version": 1,
        "status": "candidate",
        "reason": "Investigate the authorized goal.",
        "goal": "Verify the observation.",
        "steps": [
            {"step_id": "step-1", "objective": "Observe.", "completion_criteria": [criterion]}
        ],
    }


def _minimal_path(criterion: object) -> dict:
    # Model generation input: purpose+steps only. Code owns path/run/version.
    return {
        "reason": "Investigate the authorized goal.",
        "steps": [
            {"step_id": "step-1", "objective": "Observe.", "completion_criteria": [criterion]}
        ],
    }


@pytest.mark.parametrize("kind", sorted(ALLOWED_CRITERION_KINDS))
def test_generated_criteria_accept_all_canonical_kinds(kind):
    raw = _path(_criterion(kind))
    Draft202012Validator(writing_schema(PathPlan)).validate(raw)
    validate_written_criteria(raw)
    assert PathPlan.model_validate(raw).steps[0].completion_criteria[0].is_known()


def test_generation_requires_explicit_description_in_schema_and_raw_validation():
    # Intentional minimal contract: only description is required.
    criterion = _criterion()
    del criterion["description"]
    raw = _minimal_path(criterion)
    with pytest.raises(ValidationError):
        Draft202012Validator(writing_schema(PathPlan)).validate(raw)
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(raw)
    assert error.value.code == "criterion_fields_required"
    assert "description" in error.value.required_fields


def test_generation_defaults_kind_and_evidence_count_without_explicit_fields():
    # kind defaults to semantic, min_evidence to 1 in the new contract.
    raw = _minimal_path({"description": "Collect evidence of the authorized observation."})
    Draft202012Validator(writing_schema(PathPlan)).validate(raw)
    validate_written_criteria(raw)


@pytest.mark.parametrize("value", ["", "   ", "\t\n", None, 17])
def test_generated_description_must_be_nonblank(value):
    criterion = _criterion()
    criterion["description"] = value
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(_path(criterion))
    assert error.value.code == "criterion_description_required"


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", None])
def test_generated_evidence_count_is_positive_integer_without_coercion(value):
    criterion = _criterion()
    criterion["min_evidence"] = value
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(_path(criterion))
    assert error.value.code == "criterion_evidence_count_invalid"


@pytest.mark.parametrize("kind", ["hypothesis_confirmed", "hypothesis_refuted"])
@pytest.mark.parametrize("reference", [None, "", "  ", 4])
def test_hypothesis_criteria_require_reference_without_inventing_one(kind, reference):
    criterion = _criterion(kind)
    criterion["hypothesis_id"] = reference
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria({"path": _path(criterion)})
    assert error.value.code == "criterion_hypothesis_reference_required"
    assert error.value.location == "$.path.steps[0].completion_criteria[0].hypothesis_id"


def test_unknown_kind_is_rejected_before_storage_normalization():
    criterion = _criterion("secret-unrecognized-kind")
    raw = _path(criterion)
    assert PathPlan.model_validate(raw).steps[0].completion_criteria[0].kind == "unknown"
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(raw)
    feedback = error.value.correction()
    assert feedback["code"] == "criterion_kind_unsupported"
    assert set(feedback["allowed_kinds"]) == ALLOWED_CRITERION_KINDS
    assert "secret-unrecognized-kind" not in str(error.value)
    assert "secret-unrecognized-kind" not in str(feedback)


def test_legacy_text_and_defaults_still_load_with_new_generation_meaning():
    # Storage still reads legacy text as unknown; generation now accepts
    # a nonblank description as semantic instead of rejecting it.
    step = PathStep.model_validate(
        {"step_id": "legacy", "objective": "Inspect.", "completion_criteria": ["Check it."]}
    )
    assert step.completion_criteria[0].kind == "unknown"
    assert not step.completion_criteria[0].is_known()
    assert CompletionCriterion.model_validate({}).kind == "unknown"
    validate_written_criteria(_minimal_path("Check it."))
    with pytest.raises(CriterionWritingError):
        validate_written_criteria(_minimal_path("   "))


def test_writing_schema_does_not_modify_permissive_storage_schema():
    original = deepcopy(PathPlan.model_json_schema())
    generation = writing_schema(PathPlan)
    assert generation["$defs"]["CompletionCriterion"]["properties"]["kind"]["enum"]
    assert "enum" not in original["$defs"]["CompletionCriterion"]["properties"]["kind"]
    assert original == PathPlan.model_json_schema()


def test_same_schema_applies_to_root_criterion_and_nested_path_envelope():
    class Envelope(BaseModel):
        path: PathPlan | None = None

    assert writing_schema(CompletionCriterion)["required"] == [
        "description",
    ]
    assert (
        writing_schema(Envelope)["$defs"]["CompletionCriterion"]
        == (writing_schema(PathPlan)["$defs"]["CompletionCriterion"])
    )
    Draft202012Validator(writing_schema(Envelope)).validate({"path": _minimal_path(_criterion())})


def test_free_form_tool_arguments_are_not_interpreted_as_path_criteria():
    validate_written_criteria({"actions": [{"arguments": {"completion_criteria": ["tool data"]}}]})


@pytest.mark.parametrize("value", [None, 17])
def test_generated_criteria_require_array_for_non_object_input(value):
    raw = _minimal_path(_criterion())
    raw["steps"][0]["completion_criteria"] = value
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(raw)
    assert error.value.code == "criterion_list_required"


def test_generated_single_object_or_text_is_accepted_as_one_criterion():
    # Single dict/text is read as one-element array in the new contract.
    validate_written_criteria(_minimal_path("legacy-text"))
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(_minimal_path({}))
    assert error.value.code == "criterion_fields_required"


def test_invalid_kind_type_does_not_expose_rejected_input():
    criterion = _criterion()
    criterion["kind"] = {"password": "secret"}
    with pytest.raises(CriterionWritingError) as error:
        validate_written_criteria(_path(criterion))
    assert "secret" not in str(error.value.correction())


def _selected_tool_intent(**overrides: object) -> dict:
    base: dict = {
        "path_step_id": "step-1",
        "agent_id": "agent-1",
        "target_node_id": "node-1",
        "tool_id": "tool-1",
        "reason": "Use the selected tool.",
    }
    base.update(overrides)
    return base


def test_selected_tool_schema_hides_binding_and_keeps_legacy_hints_optional():
    schema = writing_schema(ActionIntent, selected_tool=True)
    properties = schema["properties"]
    assert "binding_mode" not in properties
    assert "capability" in properties
    assert "operation" in properties
    required = schema.get("required", [])
    assert "tool_id" in required
    assert "capability" not in required
    assert "operation" not in required
    Draft202012Validator(schema).validate(_selected_tool_intent())
    Draft202012Validator(schema).validate(
        _selected_tool_intent(capability="recon", operation="http_probe")
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(
            _selected_tool_intent(binding_mode="legacy_action")
        )


def test_legacy_hints_do_not_switch_selected_tool_binding():
    echoed = ActionIntent.model_validate(
        _selected_tool_intent(capability="recon", operation="http_probe")
    )
    assert echoed.binding_mode == "selected_tool"
    assert echoed.tool_id == "tool-1"
    plain = ActionIntent.model_validate(_selected_tool_intent())
    assert plain.binding_mode == "selected_tool"
