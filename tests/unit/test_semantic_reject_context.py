"""Semantic rejections keep attempt metrics and a count-only judgment shape; raw values stay out."""

import pytest

from redteam.orchestration.cognition import DecisionValidationError, StructuredDecisionClient
from redteam.orchestration.diagnostics import DecisionDiagnostic, decision_context

SENTINEL = "RAW-JUDGMENT-SENTINEL-6b2f"
TARGET = "host:RAW-TARGET-SENTINEL-91aa"

SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "updates": {"type": "array"},
        "questions": {"type": "array"},
        "termination": {
            "type": "string",
            "enum": ["continue", "held", "exhausted", "replan", "goal_achieved"],
        },
        "resume_conditions": {"type": "array"},
    },
    "required": ["reason"],
}


class Replies:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.prompts = []

    async def complete_structured(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1]["content"])
        return next(self.replies)


def _reject_once_then_accept(result):
    if result.get("questions") == [SENTINEL]:
        raise DecisionValidationError(
            DecisionDiagnostic(
                code="hypothesis_proposal_invalid",
                stage="hypothesis_planning",
                kind="validation",
                summary="Use a question that names a captured subject.",
                retryable=True,
            )
        )


async def test_semantic_reject_keeps_attempt_metrics_and_judgment_shape():
    rejected = {
        "reason": "Hold",
        "updates": [{"id": SENTINEL}],
        "questions": [SENTINEL],
        "termination": "held",
        "resume_conditions": [SENTINEL],
    }
    accepted = {
        "reason": "Hold",
        "updates": [],
        "questions": [],
        "termination": "held",
        "resume_conditions": ["An observation arrives"],
    }
    llm = Replies(rejected, accepted)
    client = StructuredDecisionClient(llm, max_attempts=2)
    result = await client.ask(
        stage="hypothesis_planning",
        payload={"known_target_ids": [TARGET]},
        response_schema=SCHEMA,
        validate_response=_reject_once_then_accept,
    )
    assert result["termination"] == "held"
    assert len(llm.prompts) == 2
    assert "Use a question that names a captured subject." in llm.prompts[1]
    assert SENTINEL not in llm.prompts[1]


async def test_semantic_reject_trace_has_metrics_shape_and_no_raw_values():
    rejected = {
        "reason": "Hold",
        "updates": [{"id": SENTINEL}],
        "questions": [SENTINEL],
        "termination": "held",
        "resume_conditions": [SENTINEL],
    }
    llm = Replies(rejected, rejected)
    client = StructuredDecisionClient(llm, max_attempts=2)
    from redteam.orchestration.cognition import DecisionFailure

    with pytest.raises(DecisionFailure) as caught:
        await client.ask(
            stage="hypothesis_planning",
            payload={"known_target_ids": [TARGET]},
            response_schema=SCHEMA,
            validate_response=_reject_once_then_accept,
        )
    rejections = [
        item for item in caught.value.diagnostic.trace if item.get("outcome") == "response_rejected"
    ]
    assert len(rejections) == 2
    first = rejections[0]
    assert isinstance(first["elapsed_ms"], int)
    assert first["response_shape"]["type"] == "object"
    assert first["response_shape"]["field_count"] == 5
    assert first["proposal_shape"]["type"] == "object"
    assert first["proposal"]["updates_shape"] == {
        "type": "array",
        "length": 1,
        "item_types": ["object"],
    }
    assert first["proposal"]["questions_shape"]["length"] == 1
    assert first["proposal"]["resume_conditions_shape"]["length"] == 1
    assert first["proposal"]["termination"] == "held"
    dumped = repr(caught.value.diagnostic.model_dump(mode="json")) + repr(
        caught.value.diagnostic.trace
    )
    assert SENTINEL not in dumped and TARGET not in dumped


def test_explicit_termination_is_kept_only_when_it_is_a_schema_member():
    payload = {"known_target_ids": [TARGET, "host:b"]}
    explicit = decision_context(
        payload,
        {"reason": "x", "termination": "held"},
        response_schema=SCHEMA,
        validated=False,
    )
    assert explicit["proposal"]["termination"] == "held"
    missing = decision_context(
        payload,
        {"reason": "x"},
        response_schema=SCHEMA,
        validated=False,
    )
    assert "termination" not in missing["proposal"]
    assert "termination_shape" not in missing["proposal"]
    invalid = decision_context(
        payload,
        {"reason": "x", "termination": SENTINEL},
        response_schema=SCHEMA,
        validated=False,
    )
    assert "termination" not in invalid["proposal"]
    assert invalid["proposal"]["termination_shape"] == {"type": "string", "length": len(SENTINEL)}
    assert SENTINEL not in repr(invalid)
    assert invalid["known_target_ids_shape"] == {
        "type": "array",
        "length": 2,
        "item_types": ["string"],
    }
    assert TARGET not in repr(invalid)


def test_absent_judgment_field_differs_from_empty_list_and_numeric_enum_is_shape_only():
    payload = {"known_target_ids": [TARGET, "host:b"]}
    empty = decision_context(
        payload,
        {"reason": "x", "questions": []},
        response_schema=SCHEMA,
        validated=False,
    )
    assert empty["proposal"]["questions_shape"] == {
        "type": "array",
        "length": 0,
        "item_types": [],
    }
    absent = decision_context(payload, {"reason": "x"}, response_schema=SCHEMA, validated=False)
    assert "questions_shape" not in absent["proposal"]
    numeric = decision_context(
        payload,
        {"reason": "x", "termination": 3},
        response_schema=SCHEMA,
        validated=False,
    )
    assert "termination" not in numeric["proposal"]
    assert numeric["proposal"]["termination_shape"] == {"type": "integer"}


def test_fixed_count_fields_keep_control_numbers_for_clipped_lists():
    payload = {
        "known_target_ids": [TARGET] * 16,
        "known_target_count": 20,
        "registered_principal_refs": [],
        "registered_principal_count": 0,
    }
    context = decision_context(payload, {"reason": "x"}, response_schema=SCHEMA, validated=False)
    assert context["known_target_count_shape"] == {"type": "integer", "value": 20}
    assert context["registered_principal_count_shape"] == {"type": "integer", "value": 0}
    assert context["known_target_ids_shape"]["length"] == 16
    assert context["registered_principal_refs_shape"] == {
        "type": "array",
        "length": 0,
        "item_types": [],
    }
    assert TARGET not in repr(context)
