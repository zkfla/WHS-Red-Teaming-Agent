"""Failure causes survive retries without copying untrusted response material."""

import json
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError as SchemaValidationError
from pydantic import BaseModel, ValidationError

from redteam.core.enums import AgentPhase
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    StructuredDecisionClient,
)
from redteam.orchestration.diagnostics import (
    DecisionDiagnostic,
    decision_context,
    diagnostic_for_exception,
    schema_diagnostic,
)
from redteam.orchestration.state import RuntimeState

SCHEMA = {
    "type": "object",
    "properties": {"kind": {"type": "string", "enum": ["evidence", "poc"]}},
    "required": ["kind"],
    "additionalProperties": False,
}


async def test_general_request_failures_keep_counts_timing_and_shallow_shapes():
    sentinel = "opaque-private-reply-material"
    llm = Replies(
        RuntimeError(sentinel), {"kind": sentinel},
        '{"kind":"evidence"}}', ' \n```json\n{"kind":"evidence"}\n```\n ',
    )
    budget = DecisionBudget(max_attempts=4)
    result = await StructuredDecisionClient(llm).ask(
        stage="next_action", payload={}, response_schema=SCHEMA, budget=budget,
    )
    assert result == {"kind": "evidence"}
    assert budget.failure_counts == {
        "llm_transport_failed": 1, "schema_enum": 1, "invalid_json": 1,
    }
    rows = [row for row in budget.trace if row["outcome"] in {
        "response_rejected", "response_validated",
    }]
    assert len(rows) == 4
    assert all(isinstance(row["elapsed_ms"], int) and row["elapsed_ms"] >= 0 for row in rows)
    assert [row["response_shape"]["type"] for row in rows] == [
        "absent", "object", "string", "string",
    ]
    assert rows[-1]["normalization"]["fence_stripped"] is True
    assert rows[-1]["normalization"]["stripped"] is True
    assert budget.last_failure is not None
    assert budget.last_failure.parse_error is not None
    assert budget.last_failure.parse_error.suffix_category == "closing_delimiter"
    assert budget.last_failure.parse_error.remaining_length == 1
    for _ in range(25):
        budget.record("next_action", "context")
    diagnostic = DecisionDiagnostic(
        code="decision_budget_exhausted", stage="next_action", kind="budget",
        summary="The request budget is exhausted.", last_cause=budget.last_failure,
        **budget.diagnostic_details(),
    )
    assert not any(row["outcome"] == "response_rejected" for row in diagnostic.trace)
    assert diagnostic.failure_counts["invalid_json"] == 1
    assert diagnostic.last_cause is not None
    assert diagnostic.last_cause.parse_error is not None
    assert diagnostic.last_cause.parse_error.remaining_length == 1
    assert sentinel not in diagnostic.model_dump_json()


def test_operational_cause_preserves_attached_diagnostic_without_exception_text():
    from redteam.orchestration.diagnostics import operational_exception_diagnostic

    original = DecisionDiagnostic(
        code="storage_unavailable", stage="evidence", kind="execution",
        summary="Evidence storage is unavailable.", retryable=False,
    )
    failure = RuntimeError("opaque-private-storage-location")
    failure.diagnostic = original  # type: ignore[attr-defined]
    diagnostic = operational_exception_diagnostic(
        failure, stage="runner", code="run_failed", summary="Run failed.",
    )
    assert diagnostic.code == "storage_unavailable"
    assert diagnostic.stage == "evidence"
    assert diagnostic.error_type == "RuntimeError"
    assert "opaque-private" not in diagnostic.model_dump_json()


async def test_empty_reply_reports_actual_fallback_parser_input_length():
    budget = DecisionBudget(max_attempts=1)
    with pytest.raises(DecisionFailure):
        await StructuredDecisionClient(Replies(" \n ")).ask(
            stage="next_action", payload={}, response_schema=SCHEMA, budget=budget,
        )
    row = next(item for item in budget.trace if item["outcome"] == "response_rejected")
    assert row["normalization"]["empty"] is True
    assert row["normalization"]["parser_input_length"] == 2
    assert row["response_shape"]["length"] == 3


def test_diagnostic_labels_use_existing_secret_masking():
    diagnostic = DecisionDiagnostic(
        code="Bearer opaquePrivateHeaderMaterial", stage="token=opaquePrivateStageMaterial",
        kind="execution", summary="Tool execution failed.",
    )
    assert "opaquePrivateHeaderMaterial" not in diagnostic.model_dump_json()
    assert "opaquePrivateStageMaterial" not in diagnostic.model_dump_json()


class Replies:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.prompts = []

    async def complete_structured(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def test_schema_retry_and_terminal_cause_never_contain_raw_instance():
    sentinel = "opaque-sensitive-response-material"
    llm = Replies({"kind": sentinel}, {"kind": sentinel})
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm, max_attempts=2).ask(
            stage="path_create",
            payload={"goal": "test"},
            response_schema=SCHEMA,
        )
    diagnostic = caught.value.diagnostic
    assert diagnostic.code == "decision_budget_exhausted"
    assert diagnostic.attempts == 2
    assert diagnostic.last_cause is not None
    assert diagnostic.last_cause.code == "schema_enum"
    assert diagnostic.last_cause.location == "$.kind"
    assert diagnostic.last_cause.attempts == 2
    assert sentinel not in diagnostic.model_dump_json()
    assert sentinel not in str(caught.value)
    assert sentinel not in llm.prompts[-1]
    assert "schema_enum at $.kind" in llm.prompts[-1]


async def test_transport_error_uses_generic_summary_not_exception_text():
    llm = Replies(RuntimeError("Bearer secret-and-response-body"))
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm, max_attempts=1).ask(
            stage="path_review",
            payload={},
            response_schema=SCHEMA,
        )
    assert caught.value.diagnostic.last_cause.code == "llm_transport_failed"
    assert "secret-and-response-body" not in caught.value.diagnostic.model_dump_json()


async def test_budget_keeps_last_actual_failure_across_calls_after_corrected_response():
    llm = Replies({"kind": "bad"}, {"kind": "evidence"})
    budget = DecisionBudget(2)
    client = StructuredDecisionClient(llm)
    assert await client.ask(
        stage="path_create",
        payload={},
        response_schema=SCHEMA,
        budget=budget,
    ) == {"kind": "evidence"}
    with pytest.raises(DecisionFailure) as caught:
        await client.ask(
            stage="path_review",
            payload={},
            response_schema=SCHEMA,
            budget=budget,
        )
    assert len(llm.prompts) == 2
    assert caught.value.stage == "path_review"
    assert caught.value.diagnostic.last_cause.stage == "path_create"
    assert caught.value.diagnostic.last_cause.code == "schema_enum"


async def test_shared_budget_records_generation_retries_and_review_exhaustion():
    budget = DecisionBudget(2)
    client = StructuredDecisionClient(Replies({"kind": "bad"}, {"kind": "evidence"}))
    await client.ask(stage="path_draft", payload={}, response_schema=SCHEMA, budget=budget)
    with pytest.raises(DecisionFailure) as caught:
        await client.ask(stage="path_review", payload={}, response_schema=SCHEMA, budget=budget)
    diagnostic = caught.value.diagnostic
    assert diagnostic.stage_attempts == {"path_draft": 2}
    assert diagnostic.attempt_limit == 2
    assert [item["outcome"] for item in diagnostic.trace] == [
        "request", "response_rejected", "request", "response_validated"
    ]


def test_decision_context_records_steps_and_proposal_without_raw_arguments():
    context = decision_context(
        {"active_path": {"steps": [{"step_id": "first", "status": "pending",
                                     "depends_on": [], "unknown_field": "not-public"}]},
         "feedback": ["password=hidden"]},
        {"mode": "actions", "actions": [{"path_step_id": "second", "tool_id": "probe",
                                           "arguments": {"password": "hidden"}}]},
    )
    encoded = json.dumps(context)
    assert "pending" in encoded and "second" in encoded
    assert "hidden" not in encoded and "not-public" not in encoded
    assert "arguments" not in encoded


def test_decision_context_accepts_structured_terminal_feedback_without_copying_contents():
    context = decision_context(
        {"feedback": {"code": "terminal_reference_invalid", "private": "opaque-private"}},
        {"accepted": True}, validated=False,
    )
    assert context["feedback"] == []
    assert context["feedback_shape"] == {"type": "object", "field_count": 2}
    assert context["proposal"]["accepted_shape"] == {"type": "boolean"}
    assert "opaque-private" not in json.dumps(context)


def test_markdown_report_exposes_durable_planning_cause_and_legacy_trace_gap():
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.orchestration.reporting import TerminalReportProjection

    report = TerminalReportProjection(
        run_id="run-test", phase=AgentPhase.FAILED, termination_reason="planning_error",
        actions_executed=0, evidence_count=0, finding_count=0, ready_for_release=False,
    )
    diagnostic = DecisionDiagnostic(
        code="decision_budget_exhausted", stage="next_action", kind="budget", attempts=3,
        summary="budget exhausted", last_cause=DecisionDiagnostic(
            code="step_prerequisites_unmet", stage="next_action", kind="format",
            summary="prerequisite step not completed: first",
        ),
    )
    report = report.model_copy(update={"planning_diagnostic": diagnostic})
    text = _render_markdown(report)
    assert "prerequisite step not completed: first" in text
    assert "Detailed decision trace was not recorded" in text


async def test_json_decode_error_does_not_store_raw_body():
    llm = Replies("{'cookie': 'secret-body'}")
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm, max_attempts=1).ask(
            stage="path_create",
            payload={},
            response_schema=SCHEMA,
        )
    assert caught.value.diagnostic.last_cause.code == "invalid_json"
    assert "secret-body" not in caught.value.diagnostic.model_dump_json()


@pytest.mark.parametrize("provider_parse", [False, True])
async def test_json_rejection_records_decoder_coordinates_without_response_contents(provider_parse):
    body = '{\n"opaque-private": }'
    try:
        json.loads(body)
    except json.JSONDecodeError as exc:
        expected = exc
    response = expected if provider_parse else "```json\n" + body + "\n```"
    llm = Replies(response)
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm, max_attempts=1).ask(
            stage="next_action", payload={}, response_schema=SCHEMA,
        )
    diagnostic = caught.value.diagnostic
    rejected = next(row for row in diagnostic.trace if row["outcome"] == "response_rejected")
    assert rejected["parse_error"] == {
        "coordinate_source": "decoder_input", "line": expected.lineno,
        "column": expected.colno, "position": expected.pos,
        "input_length": len(body), "syntax_reason": "expected_value",
        "at_input_end": False, "start_category": "object",
        "first_value_end": None, "remaining_length": len(body),
        "framing": "undecodable", "suffix_category": "other",
    }
    assert diagnostic.last_cause.validation_issues == [{
        "rule": "json_decode",
        "location": f"decoder input line {expected.lineno}, column {expected.colno}",
    }]
    assert len(llm.prompts) == 1
    assert "opaque-private" not in diagnostic.model_dump_json()


def _decode_error(body: str) -> json.JSONDecodeError:
    try:
        json.loads(body)
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError(f"body parses cleanly: {body!r}")


async def test_json_syntax_reasons_map_real_decoder_messages():
    truncated = _decode_error('{"a": 1')
    assert truncated.pos >= len(truncated.doc)
    diagnostic = diagnostic_for_exception(truncated, stage="next_action")
    assert diagnostic.code == "invalid_json"
    assert diagnostic.kind == "format"
    assert diagnostic.retryable is True
    assert diagnostic.validation_issues == [{
        "rule": "json_decode",
        "location": f"decoder input line {truncated.lineno}, column {truncated.colno}",
    }]
    assert "Return a valid JSON object" in diagnostic.summary
    assert truncated.msg not in diagnostic.model_dump_json()
    assert truncated.doc not in diagnostic.model_dump_json()

    sentinel = "opaqueControlFragment"
    control = _decode_error('"ab' + sentinel + '\x01cd"')
    context = diagnostic_for_exception(control, stage="next_action")
    assert context.code == "invalid_json"
    assert "Return a valid JSON object" in context.summary
    # Letters survive JSON serialization unchanged, so a copied fragment
    # cannot hide behind quote or control-character escaping.
    assert sentinel not in context.model_dump_json()

    escape = _decode_error('{"a": "x\\d"}')
    escape_diagnostic = diagnostic_for_exception(escape, stage="next_action")
    assert escape_diagnostic.code == "invalid_json"
    assert "Use valid JSON escapes" in escape_diagnostic.summary


async def test_json_unknown_syntax_never_copies_raw_message():
    sentinel = "opaque-sentinel-syntax-msg"
    body = '{"kind": "' + sentinel + '"}'
    exc = json.JSONDecodeError("mystery decoder failure " + sentinel, body, 3)
    diagnostic = diagnostic_for_exception(exc, stage="next_action")
    assert diagnostic.code == "invalid_json"
    assert diagnostic.summary.startswith("Return a valid JSON object")
    dumped = diagnostic.model_dump_json()
    assert sentinel not in dumped
    assert "mystery decoder failure" not in dumped
    assert diagnostic.feedback().startswith("invalid_json")
    assert sentinel not in diagnostic.feedback()


def test_schema_unknown_property_location_is_not_copied():
    error = SchemaValidationError("raw response", validator="type", path=["kind", "raw-secret-key"])
    diagnostic = schema_diagnostic(error, SCHEMA, stage="path_create")
    assert diagnostic.location == "$.kind"
    assert "raw-secret" not in diagnostic.model_dump_json()
    assert "raw response" not in diagnostic.model_dump_json()


def test_pydantic_validation_excludes_input_and_message():
    class Reply(BaseModel):
        kind: int

    with pytest.raises(ValidationError) as caught:
        Reply.model_validate({"kind": "password=secret-input"})
    diagnostic = diagnostic_for_exception(caught.value, stage="path_create")
    assert diagnostic.location == "$.kind"
    assert "secret-input" not in diagnostic.model_dump_json()


def test_pydantic_owner_schema_extends_locations_without_copying_input():
    from redteam.orchestration.graph_recording import GraphRecordingInput

    secret = "opaque-recording-value"
    with pytest.raises(ValidationError) as caught:
        GraphRecordingInput.model_validate(
            {
                "facts": [
                    {
                        "evidence_id": "ev-1",
                        "value": secret,
                        "fact_name": "banner",
                        "target_node_id": "host:target.local",
                    }
                ]
            }
        )
    # Without the owner schema the shared decision allowlist does not know
    # this contract's nested names, so the location stops at the root.
    default = diagnostic_for_exception(caught.value, stage="native_local_tool")
    assert default.location == "$"
    assert default.validation_issues[0] == {"rule": "missing", "location": "$"}
    scoped = diagnostic_for_exception(
        caught.value,
        stage="native_local_tool",
        schema=GraphRecordingInput.model_json_schema(),
    )
    assert scoped.code == "model_validation"
    assert scoped.validation_issues[0] == {
        "rule": "missing",
        "location": "$.facts[0].raw_path",
    }
    assert "$.facts[0].raw_path" in scoped.feedback()
    assert secret not in scoped.model_dump_json()


def test_pydantic_owner_schema_still_hides_undeclared_keys():
    from redteam.orchestration.graph_recording import GraphRecordingInput

    secret = "opaque-undeclared-value"
    with pytest.raises(ValidationError) as caught:
        GraphRecordingInput.model_validate(
            {"facts": [], "mystery_key_name": secret}
        )
    scoped = diagnostic_for_exception(
        caught.value,
        stage="native_local_tool",
        schema=GraphRecordingInput.model_json_schema(),
    )
    assert scoped.location == "$"
    assert scoped.validation_issues == [{"rule": "extra_forbidden", "location": "$"}]
    dumped = scoped.model_dump_json()
    assert "mystery_key_name" not in dumped
    assert secret not in dumped


@pytest.mark.parametrize(
    ("read_input", "field", "rule"),
    [
        ({"evidence_ids": ["opaque-private-value"]}, "evidence_id", "missing"),
        ({"evidence_id": ["opaque-private-value"]}, "evidence_id", "string_type"),
        ({"evidence_id": "opaque-private-value", "offset": -1}, "offset", "greater_than_equal"),
        ({"evidence_id": "opaque-private-value", "limit": 20001}, "limit", "less_than_equal"),
    ],
)
def test_read_rejections_preserve_public_rule_location_and_input_shape(read_input, field, rule):
    from redteam.orchestration.evidence_access import EvidenceReadRequest

    with pytest.raises(ValidationError) as caught:
        EvidenceReadRequest.model_validate(read_input)
    diagnostic = diagnostic_for_exception(caught.value, stage="next_action")
    assert diagnostic.validation_issues[0] == {"rule": rule, "location": f"$.{field}"}
    assert rule in diagnostic.feedback() and field in diagnostic.feedback()
    context = decision_context({}, {"mode": "read_evidence", "evidence_request": read_input})
    shape = context["proposal"]["evidence_request_shape"]
    if field in {"offset", "limit"}:
        assert shape["fields"][field]["value"] == read_input[field]
    elif isinstance(read_input.get("evidence_id"), list):
        assert shape["fields"]["evidence_id"]["type"] == "array"
    else:
        assert shape["unknown_field_count"] == 1
        assert shape["unknown_field_shapes"][0]["type"] == "array"
    assert "opaque-private-value" not in json.dumps(context)
    assert "opaque-private-value" not in diagnostic.model_dump_json()


async def test_schema_rejection_records_shape_before_validation_and_keeps_retry_cause_in_report():
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.orchestration.evidence_access import EvidenceReadRequest
    from redteam.orchestration.reporting import TerminalReportProjection

    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["read_evidence"]},
            "evidence_request": EvidenceReadRequest.model_json_schema(),
        },
        "required": ["mode", "evidence_request"],
    }
    llm = Replies({"mode": "read_evidence", "evidence_request": ["opaque-private-value"]})
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm, max_attempts=1).ask(
            stage="next_action", payload={}, response_schema=schema,
        )
    diagnostic = caught.value.diagnostic
    rejection = next(item for item in diagnostic.trace if item["outcome"] == "response_rejected")
    assert rejection["proposal"]["evidence_request_shape"]["type"] == "array"
    assert rejection["validation_issues"] == [{"rule": "type", "location": "$.evidence_request"}]
    assert diagnostic.last_cause.validation_issues == rejection["validation_issues"]
    assert len(llm.prompts) == 1
    report = TerminalReportProjection(
        run_id="run-safe", phase=AgentPhase.FAILED, termination_reason="planning_error",
        actions_executed=0, evidence_count=0, finding_count=0, ready_for_release=False,
        planning_diagnostic=diagnostic,
    )
    for rendered in (report.model_dump_json(), _render_markdown(report)):
        assert "$.evidence_request" in rendered
        assert "opaque-private-value" not in rendered


def test_read_shape_does_not_copy_unknown_keys_queries_or_convert_bad_control_types():
    from redteam.orchestration.diagnostics import evidence_request_shape

    shape = evidence_request_shape({
        "query": "opaque-private-value", "opaque-private-key": {"password": "secret"},
        "offset": True, "limit": "20001",
    })
    assert shape["fields"]["offset"] == {"type": "boolean"}
    assert shape["fields"]["limit"] == {"type": "string", "length": 5}
    assert shape["unknown_field_count"] == 1
    assert "opaque-private" not in json.dumps(shape)
    assert "secret" not in json.dumps(shape)
    assert evidence_request_shape({"limit": float("inf")})["fields"]["limit"] == {
        "type": "number", "finite": False,
    }


async def test_prevalidation_projection_never_copies_malformed_public_reference_or_reason():
    schema = {
        "type": "object",
        "properties": {"reason": {"type": "string"}, "path_step_id": {"type": "string"}},
        "required": ["reason"],
    }
    llm = Replies({
        "reason": {"opaque-private-key": "opaque-private-value"},
        "path_step_id": ["opaque-private-value"],
        "actions": [{"tool_id": {"opaque-private-key": "opaque-private-value"}}],
    })
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm, max_attempts=1).ask(
            stage="next_action", payload={}, response_schema=schema,
        )
    rejected = next(
        item for item in caught.value.diagnostic.trace if item["outcome"] == "response_rejected"
    )
    assert rejected["proposal"]["reason_shape"]["type"] == "object"
    assert rejected["proposal"]["path_step_id_shape"]["type"] == "array"
    assert "opaque-private" not in caught.value.diagnostic.model_dump_json()


async def test_custom_planning_provider_rejection_keeps_final_shape_and_rules_without_fake_usage():
    from dataclasses import replace

    from redteam.orchestration.engine import InvestigationEngine
    from tests.fakes import build_test_application

    class CustomProvider:
        async def propose(self, request):
            return {"reason": {"opaque-private-key": "opaque-private-value"}}

    engine = InvestigationEngine(
        replace(build_test_application(), planning_provider=CustomProvider())
    )
    budget = DecisionBudget(1)
    with pytest.raises(ValueError) as caught:
        await engine._propose(
            stage="next_action", payload={}, budget=budget,
            response_schema={
                "type": "object", "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        )
    diagnostic = diagnostic_for_exception(caught.value, stage="next_action")
    assert diagnostic.code == "schema_type" and diagnostic.location == "$.reason"
    assert diagnostic.trace[0]["diagnostic_context"]["proposal"]["reason_shape"]["type"] == "object"
    assert diagnostic.validation_issues == [{"rule": "type", "location": "$.reason"}]
    assert budget.used == budget.tokens_used == budget.unknown_usage_count == 0
    assert budget.normal_used == 1
    assert "opaque-private" not in diagnostic.model_dump_json()


@pytest.mark.parametrize(
    ("operation", "tool_id", "code"),
    [
        ("opaque-unsupported-action", "web.request", "unknown_operation"),
        ("http_request", "recon.probe", "tool_outside_capability_namespace"),
    ],
)
def test_action_validation_preserves_safe_correction_code(operation, tool_id, code):
    from redteam.orchestration.agents.base import PlanStep

    with pytest.raises(ValidationError) as caught:
        PlanStep.model_validate(
            {
                "step_id": "safe-step",
                "capability": "web",
                "operation": operation,
                "tool_id": tool_id,
                "target": {"kind": "host", "host": "localhost"},
                "arguments": {"password": "secret-input"},
                "reason": "Authorized local inspection",
            }
        )
    diagnostic = diagnostic_for_exception(caught.value, stage="action_compilation")
    assert diagnostic.code == code
    assert "secret-input" not in diagnostic.model_dump_json()
    assert "opaque-unsupported-action" not in diagnostic.model_dump_json()


def test_legacy_checkpoint_defaults_and_diagnostic_roundtrip():
    runtime = RuntimeState(run_id="run-safe", engagement_id="eng-safe")
    assert runtime.planning_diagnostic is None
    assert runtime.planning_diagnostic_history == []
    diagnostic = DecisionDiagnostic(
        code="schema_enum",
        kind="format",
        stage="path_create",
        attempts=2,
        location="$.kind",
        summary="Use an allowed value.",
        retryable=True,
    )
    runtime = runtime.model_copy(
        update={
            "planning_diagnostic": diagnostic,
            "planning_diagnostic_history": [diagnostic],
        }
    )
    restored = RuntimeState.model_validate_json(runtime.model_dump_json())
    assert restored.planning_diagnostic == diagnostic
    assert restored.planning_diagnostic_history == [diagnostic]


async def test_plan_failure_then_replan_preserves_structured_cause(monkeypatch):
    from redteam.orchestration.nodes.plan import plan_node
    from redteam.orchestration.nodes.replan import replan_node

    events = []

    class Publisher:
        async def publish(self, event_type, payload, **kwargs):
            events.append((event_type, payload))
            return SimpleNamespace(event_id="event-safe")

    diagnostic = DecisionDiagnostic(
        code="criterion_hypothesis_required",
        stage="path_create",
        kind="format",
        location="$.steps[0].completion_criteria[0].hypothesis_id",
        summary="Include a hypothesis reference.",
        attempts=2,
        retryable=True,
    )

    class Engine:
        last_event_id = None
        # New contract stub: production engine always carries this.
        updated_runtime = None

        def __init__(self, deps):
            pass

        async def decide(self, *args, **kwargs):
            raise DecisionFailure("path_create", "format", 2, diagnostic=diagnostic)

    monkeypatch.setattr("redteam.orchestration.nodes.plan.InvestigationEngine", Engine)
    monkeypatch.setattr("redteam.orchestration.nodes.plan.publisher_for", lambda *a: Publisher())
    monkeypatch.setattr("redteam.orchestration.nodes.replan.publisher_for", lambda *a: Publisher())
    runtime = RuntimeState(run_id="run-safe", engagement_id="eng-safe")
    state = await plan_node({"runtime": runtime}, deps=SimpleNamespace())
    assert state["runtime"].planning_diagnostic == diagnostic
    assert events[0][1]["diagnostic"]["code"] == "criterion_hypothesis_required"
    state = await replan_node(state, deps=SimpleNamespace())
    assert state["runtime"].phase is AgentPhase.FAILED
    assert state["runtime"].planning_diagnostic == diagnostic
    assert state["runtime"].planning_diagnostic_history == [diagnostic]
    assert events[-1][1]["diagnostic"]["location"] == diagnostic.location
    assert "criterion_hypothesis_required" in state["runtime"].planning_feedback[-1]
    assert "raw" not in json.dumps(events)


async def test_successful_plan_clears_current_diagnostic_and_retains_history(monkeypatch):
    from redteam.orchestration.nodes.plan import plan_node

    diagnostic = DecisionDiagnostic(
        code="schema_type",
        stage="path_create",
        kind="format",
        summary="Use the required type.",
    )

    class Engine:
        last_event_id = None
        # New contract stub: production engine always carries this.
        updated_runtime = None

        def __init__(self, deps):
            pass

        async def decide(self, *args, **kwargs):
            return None, [], "hold"

    monkeypatch.setattr("redteam.orchestration.nodes.plan.InvestigationEngine", Engine)
    monkeypatch.setattr("redteam.orchestration.nodes.plan.publisher_for", lambda *a: None)
    runtime = RuntimeState(
        run_id="run-safe",
        engagement_id="eng-safe",
        planning_error="DecisionFailure",
        planning_diagnostic=diagnostic,
        planning_diagnostic_history=[diagnostic],
    )
    state = await plan_node({"runtime": runtime}, deps=SimpleNamespace())
    assert state["runtime"].planning_diagnostic is None
    assert state["runtime"].planning_error is None
    assert state["runtime"].planning_diagnostic_history == [diagnostic]


def test_redaction_precedes_truncation_and_checkpoint_history_is_bounded():
    diagnostic = DecisionDiagnostic(
        code="validation",
        stage="path_create",
        kind="format",
        summary="Bearer hidden-value " + "x" * 600,
    )
    assert len(diagnostic.summary) == 500
    assert "hidden-value" not in diagnostic.summary
    runtime = RuntimeState(
        run_id="run-safe",
        engagement_id="eng-safe",
        planning_diagnostic_history=[diagnostic] * 15,
        planning_feedback=["password=hidden-value " + "x" * 600] * 15,
    )
    assert len(runtime.planning_diagnostic_history) == 10
    assert len(runtime.planning_feedback) == 10
    assert all(len(item) == 500 for item in runtime.planning_feedback)
    assert "hidden-value" not in runtime.model_dump_json()


@pytest.mark.parametrize("verdict", ["allow", "deny", "require_approval"])
async def test_replan_feedback_keeps_rejections_without_treating_permission_as_failure(verdict):
    from datetime import datetime, timezone

    from redteam.core.contracts.policy import PolicyDecision
    from redteam.core.enums import PolicyVerdict
    from redteam.orchestration.nodes.replan import replan_node
    from redteam.orchestration.state import initial_graph_state
    from tests.fakes import build_test_application

    explanation = "policy explanation"
    state = initial_graph_state(RuntimeState(run_id="run-safe", engagement_id="eng-safe"))
    state["policy_decision"] = PolicyDecision(
        action_id="action-safe", verdict=PolicyVerdict(verdict),
        reasons=[explanation], evaluated_at=datetime.now(timezone.utc),
    )
    result = await replan_node(state, deps=build_test_application())
    assert (explanation in result["runtime"].planning_feedback) == (verdict != "allow")
    assert state["policy_decision"].reasons == [explanation]
