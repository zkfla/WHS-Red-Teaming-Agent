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
