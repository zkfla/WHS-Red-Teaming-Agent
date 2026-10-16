"""Result publication invariants expose stable, safe diagnostics."""

import pytest
from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from redteam.core.contracts.mission import InvestigationResult
from redteam.orchestration.central_results import ResultPublication
from redteam.orchestration.diagnostics import diagnostic_for_exception


def _publication(**result_changes):
    result = {
        "result_id": "result-a",
        "outcome": "inconclusive",
        "summary": "A safe summary",
        "questions": [],
    }
    result.update(result_changes)
    return {"result": result}


@pytest.mark.parametrize(
    ("changes", "code", "location"),
    [
        ({"result_id": "  "}, "result_id_blank", "$.result.result_id"),
        ({"summary": "  "}, "result_summary_blank", "$.result.summary"),
        (
            {"questions": [
                {"question_id": "same", "question": "First"},
                {"question_id": "same", "question": "Second"},
            ]},
            "result_question_duplicate",
            "$.result.questions",
        ),
        (
            {"contract_version": 1, "observations": ["private-observation-marker"]},
            "result_v1_extensions_forbidden",
            "$.result.contract_version",
        ),
        (
            {"questions": [{"question_id": " ", "question": "Question"}]},
            "question_id_blank",
            "$.result.questions[0].question_id",
        ),
        (
            {"questions": [{"question_id": "q1", "question": "  "}]},
            "question_text_blank",
            "$.result.questions[0].question",
        ),
        (
            {"questions": [{
                "question_id": "q1", "question": "Question",
                "required_for_goal": True, "reason": "  ",
            }]},
            "question_required_reason_missing",
            "$.result.questions[0].reason",
        ),
        (
            {"observations": ["  "]},
            "result_observation_blank",
            "$.result.observations",
        ),
        (
            {"limitations": ["  "]},
            "result_observation_blank",
            "$.result.limitations",
        ),
    ],
)
def test_publication_invariants_produce_safe_owner_diagnostics(changes, code, location):
    with pytest.raises(ValidationError) as failed:
        ResultPublication.model_validate(_publication(**changes))

    diagnostic = diagnostic_for_exception(
        failed.value,
        stage="result_publication",
        schema=ResultPublication.model_json_schema(),
    )

    assert diagnostic.code == code
    assert diagnostic.location == location
    assert diagnostic.validation_issues == [{"rule": code, "location": location}]
    assert diagnostic.repair_condition == diagnostic.summary
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.owner == "core.contracts.mission"
    serialized = diagnostic.model_dump_json()
    assert "private-observation-marker" not in serialized
    assert "result-a" not in serialized
    assert "Question" not in serialized


def test_diagnostic_discards_untrusted_location_labels_for_owner_codes():
    error = ValidationError.from_exception_data(
        "ResultPublication",
        [
            InitErrorDetails(
                type=PydanticCustomError(
                    "result_observation_blank", "untrusted detail"
                ),
                loc=("private-marker",),
                input=None,
            )
        ],
    )

    diagnostic = diagnostic_for_exception(
        error,
        stage="result_publication",
        schema=ResultPublication.model_json_schema(),
    )

    assert diagnostic.location == "$"
    assert "private-marker" not in diagnostic.model_dump_json()
    assert "untrusted detail" not in diagnostic.model_dump_json()


def test_result_publication_schema_fields_and_v1_serialization_stay_compatible():
    schema = ResultPublication.model_json_schema()
    result_schema = schema["$defs"]["InvestigationResult"]
    valid_v1 = InvestigationResult.model_validate(
        _publication()["result"]
    )

    assert set(result_schema["properties"]) == {
        "result_id", "outcome", "summary", "evidence_ids", "hypothesis_ids",
        "finding_node_ids", "completion_receipt_id", "questions", "contract_version",
        "observations", "limitations",
    }
    assert valid_v1.model_dump() == {
        "result_id": "result-a",
        "outcome": "inconclusive",
        "summary": "A safe summary",
        "evidence_ids": [],
        "hypothesis_ids": [],
        "finding_node_ids": [],
        "completion_receipt_id": None,
        "questions": [],
    }

