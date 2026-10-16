from __future__ import annotations

import json

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import StructuredCompletion
from redteam.orchestration.cognition import DecisionValidationError, StructuredDecisionClient
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.llm_requests import (
    LLM_REQUEST_CONTENT_LIMIT,
    RequestAdmissionError,
    RunRequestLedger,
)
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from tests.unit.test_orchestration_coordination import RUN, world

SECRET_PROSE = "MODEL_VALIDATION_PROSE_MUST_NOT_BE_PERSISTED"


class StructuredScripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete_structured_with_usage(self, **kwargs):
        assert kwargs["num_retries"] == 0
        self.calls.append(kwargs)
        return StructuredCompletion(value=self.responses.pop(0), total_tokens=7)


async def _ledger(tmp_path):
    workspace = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(workspace.tasks, workspace.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    return ledger


async def _rows(ledger):
    async def read(tx):
        return RunRequestLedger._rows(tx, RUN)
    return await ledger.coordinator.change(read)


async def test_two_planner_rejections_are_durably_linked_across_truncated_previews(tmp_path):
    ledger = await _ledger(tmp_path)
    provider = StructuredScripted([
        {"decision": "reject-first", "extra": "present"},
        {"decision": "format-reject"},
        {"decision": "accept", "extra": "present"},
    ])
    client = StructuredDecisionClient(RunMeteredLLM(provider, ledger), max_attempts=3)
    schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string"},
            "extra": {"type": "string"},
        },
        "required": ["decision", "extra"],
        "additionalProperties": False,
    }
    diagnostic_by_decision = {"reject-first": "planner_rule_first"}

    def validate_response(value):
        code = diagnostic_by_decision.get(value["decision"])
        if code is not None:
            raise DecisionValidationError(DecisionDiagnostic(
                code=code,
                stage="hypothesis_planning",
                kind="validation",
                location="$.decision",
                summary=f"{SECRET_PROSE} {value['decision']}",
                retryable=True,
                validation_issues=[{"rule": code, "location": "$.decision"}],
            ))

    with llm_request_scope(RUN, "planner"):
        result = await client.ask(
            stage="hypothesis_planning",
            payload={"planning_context": "x" * 70_000},
            response_schema=schema,
            validate_response=validate_response,
        )

    assert result == {"decision": "accept", "extra": "present"}
    records = sorted(await _rows(ledger), key=lambda row: row["request_sequence"])
    assert len(provider.calls) == len(records) == 3
    assert [row["decision_outcome"] for row in records] == [
        "semantic_rejected", "format_rejected", "accepted",
    ]
    assert [row["attempt_kind"] for row in records] == [
        "normal", "semantic_repair", "format_repair",
    ]
    first_id, second_id, accepted_id = [row["request_id"] for row in records]
    assert "previous_rejection_request_id" not in records[0]
    assert records[1]["previous_rejection_request_id"] == first_id
    assert records[2]["previous_rejection_request_id"] == second_id
    assert records[0]["decision_diagnostic"] == {
        "code": "planner_rule_first",
        "stage": "hypothesis_planning",
        "location": "$.decision",
        "rules": [{"rule": "planner_rule_first", "location": "$.decision"}],
    }
    assert records[1]["decision_diagnostic"] == {
        "code": "schema_required",
        "stage": "hypothesis_planning",
        "location": "$.extra",
        "rules": [{"rule": "required", "location": "$.extra"}],
    }
    for row in records:
        assert row["request_truncated"] is True
        assert len(row["request_content"]) == LLM_REQUEST_CONTENT_LIMIT
    assert SECRET_PROSE not in json.dumps(records)

    # Replaying identical classification metadata is idempotent.
    await ledger.classify_decision(
        RUN, accepted_id, purpose="planning", outcome="accepted", valid=True,
        made_progress=True, previous_rejection_request_id=second_id,
    )
    accepted = next(row for row in await _rows(ledger) if row["request_id"] == accepted_id)
    assert accepted["previous_rejection_request_id"] == second_id


async def test_repair_diagnostic_summary_rejects_prose_and_unbounded_fields(tmp_path):
    ledger = await _ledger(tmp_path)
    request_id = await ledger.reserve(
        RUN, owner="planner", kind="complete_structured", stage="hypothesis_planning",
        purpose="planning", attempt_kind="normal",
    )
    await ledger.dispatch(RUN, request_id)
    await ledger.settle(RUN, request_id, total_tokens=1)
    await ledger.classify_decision(
        RUN, request_id, outcome="semantic_rejected", valid=False,
        decision_diagnostic={
            "code": "planner_rule_failed",
            "stage": "hypothesis_planning",
            "location": "$.decision",
            "rules": [{"rule": "required", "location": "$.decision"}],
        },
    )
    malformed = {
        "code": "model prose with arbitrary content",
        "stage": "hypothesis_planning",
        "location": "$.decision",
        "rules": [],
    }
    with pytest.raises(RequestAdmissionError, match="classification_invalid"):
        await ledger.classify_decision(
            RUN, request_id, outcome="semantic_rejected", valid=False,
            decision_diagnostic=malformed,
        )
