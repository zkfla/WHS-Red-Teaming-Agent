"""Phase-3 input-event diagnostic projection (TASK-01 web)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.app.web.events import EventLog, public_payload
from redteam.app.web.frontend import WebFrontend, _diagnostic_view
from redteam.orchestration.diagnostics import DecisionDiagnostic


def _cause() -> DecisionDiagnostic:
    return DecisionDiagnostic(
        code="provider_rate_limited",
        stage="conversation_turn",
        kind="transport",
        attempts=2,
        summary="provider throttled the request",
        provider_status=429,
        location="$.responses[0]",
        validation_issues=[{"rule": "schema_required_field", "location": "$.mode"}],
    )


def _diagnostic() -> DecisionDiagnostic:
    return DecisionDiagnostic(
        code="conversation_failed",
        stage="conversation_turn",
        kind="transport",
        attempts=3,
        summary="assistant turn failed",
        last_cause=_cause(),
    )


def _receipt(diagnostic=None, reason="ConversationError"):
    envelope = SimpleNamespace(
        input_id="in-1",
        text="hello",
        received_at=datetime.now(timezone.utc),
        binding=None,
    )
    return SimpleNamespace(
        envelope=envelope,
        decision=None,
        state="not_applied",
        reason=reason,
        diagnostic=diagnostic,
    )


def _controller(*receipts):
    return SimpleNamespace(
        receipts={f"in-{index}": receipt for index, receipt in enumerate(receipts)}
    )


def test_view_flattens_nested_cause_to_shallow_scalars() -> None:
    view = _diagnostic_view(_diagnostic())
    assert view is not None
    assert view["rule"] == "schema_required_field"
    assert view["location"] == "$.responses[0]"
    assert view["provider_status"] == 429
    assert view["cause_code"] == "provider_rate_limited"
    assert "provider_rate_limited" in view["label"]
    assert "status 429" in view["label"]
    for value in view.values():
        assert not isinstance(value, (dict, list))
def test_view_none_projects_none() -> None:
    assert _diagnostic_view(None) is None


def test_absent_fields_stay_absent_never_none_string() -> None:
    bare = DecisionDiagnostic(
        code="conversation_failed",
        stage="conversation_turn",
        kind="transport",
        summary="assistant turn failed",
    )
    view = _diagnostic_view(bare)
    assert view is not None
    assert "location" not in view
    assert "rule" not in view
    assert "provider_status" not in view
    assert "cause_code" not in view
    assert "None" not in " ".join(str(value) for value in view.values())


def _published_data(receipt) -> dict:
    log = EventLog()
    WebFrontend(log)._publish_inputs(_controller(receipt))
    tail = log.tail()
    assert len(tail) == 1
    assert tail[0]["kind"] == "input"
    return tail[0]["data"]


def test_tail_carries_flattened_cause_fields() -> None:
    data = _published_data(_receipt(_diagnostic()))
    projected = data["diagnostic"]
    assert projected["rule"] == "schema_required_field"
    assert projected["location"] == "$.responses[0]"
    assert projected["provider_status"] == 429
    assert projected["cause_code"] == "provider_rate_limited"
    assert projected["code"] == "conversation_failed"
    assert projected["attempts"] == 3


def test_typename_only_reason_is_replaced_by_label() -> None:
    data = _published_data(_receipt(_diagnostic()))
    assert data["reason"] != "ConversationError"
    assert "schema_required_field" in data["reason"]
    assert "status 429" in data["reason"]


def test_informative_reason_is_preserved() -> None:
    data = _published_data(_receipt(_diagnostic(), reason="waiting for operator"))
    assert data["reason"] == "waiting for operator"
    assert data["diagnostic"]["rule"] == "schema_required_field"


def test_signature_catches_change_beyond_label_truncation() -> None:
    log = EventLog()
    frontend = WebFrontend(log)

    def cause_with_location(location: str) -> DecisionDiagnostic:
        return DecisionDiagnostic(
            code="provider_rate_limited",
            stage="conversation_turn",
            kind="transport",
            summary="provider throttled the request",
            provider_status=429,
            location=location,
            validation_issues=[{"rule": "R" * 600, "location": "$.mode"}],
        )

    def outer_with_location(location: str) -> DecisionDiagnostic:
        return DecisionDiagnostic(
            code="conversation_failed",
            stage="conversation_turn",
            kind="transport",
            attempts=3,
            summary="assistant turn failed",
            last_cause=cause_with_location(location),
        )

    receipt = _receipt(outer_with_location("$.responses[0]"))
    controller = _controller(receipt)
    frontend._publish_inputs(controller)
    first_label = log.tail()[-1]["data"]["reason"]
    receipt.diagnostic = outer_with_location("$.other[5]")
    frontend._publish_inputs(controller)
    tail = log.tail()
    assert len(tail) == 2
    assert tail[-1]["data"]["reason"] == first_label
    assert tail[-1]["data"]["diagnostic"]["location"] == "$.other[5]"


def _live_controller(frontend):
    import io as std_io

    from rich.console import Console

    from redteam.app.cli.conversation import ConversationDecision
    from redteam.app.cli.session_controller import SessionController
    from redteam.app.cli.session_types import RunResult

    class Conversation:
        async def decide(self, text, **kwargs):
            return ConversationDecision(mode="answer", response="ok")

    async def runner(objective, controller):
        return RunResult()

    return SessionController(
        console=Console(file=std_io.StringIO(), width=80, color_system=None),
        frontend=frontend,
        conversation=Conversation(),
        runner=runner,
        engagement_name="eng",
        scope=["localhost"],
    )


def test_snapshot_items_preserve_flattened_cause_through_real_controller() -> None:
    from redteam.app.cli.conversation import ConversationError
    from redteam.app.cli.session_controller import input_failure_diagnostic

    log = EventLog()
    frontend = WebFrontend(log)
    controller = _live_controller(frontend)
    controller.submit("hi")
    receipt = controller.receipts["input_1"]
    exc = ConversationError("conversation decision failed", diagnostic=_diagnostic())
    receipt.diagnostic = input_failure_diagnostic(exc)
    frontend.invalidate()
    snapshot_items = log.tail()
    data = [item["data"] for item in snapshot_items if item["kind"] == "input"][-1]
    projected = data["diagnostic"]
    assert projected["rule"] == "schema_required_field"
    assert projected["location"] == "$.responses[0]"
    assert projected["provider_status"] == 429
    assert "status 429" in data["reason"]
    assert data["reason"] != "ConversationError"


def test_secret_marker_in_safe_fields_is_masked_out() -> None:
    marker = "hunter2-secret-value"
    cause = DecisionDiagnostic(
        code="provider_rate_limited",
        stage="conversation_turn",
        kind="transport",
        summary=f"provider note {marker}",
        provider_status=429,
        location=f"$.responses[0] password={marker}",
        validation_issues=[{"rule": "schema_required_field", "location": "$.mode"}],
    )
    diagnostic = DecisionDiagnostic(
        code="conversation_failed",
        stage="conversation_turn",
        kind="transport",
        summary=f"assistant note {marker}",
        last_cause=cause,
    )
    view = _diagnostic_view(diagnostic)
    assert view is not None
    blob = json.dumps(view)
    assert marker not in blob
    assert marker not in (view.get("label") or "")
    assert view["rule"] == "schema_required_field"
    assert view["provider_status"] == 429


def test_sse_round_trip_preserves_flattened_fields() -> None:
    import redteam.app.web.events as web_events

    data = _published_data(_receipt(_diagnostic()))
    log = EventLog()
    item = log.append("input", data)
    wire = web_events.sse_message(item)
    restored = json.loads(wire["data"])
    projected = restored["data"]["diagnostic"]
    assert projected["rule"] == "schema_required_field"
    assert projected["location"] == "$.responses[0]"
    assert projected["provider_status"] == 429


def test_other_payloads_keep_depth_and_size_bounds() -> None:
    from redteam.app.web.events import DEPTH_LIMIT, KEY_LIMIT, LIST_LIMIT

    nested: dict = {}
    cursor = nested
    for _ in range(DEPTH_LIMIT + 3):
        child: dict = {}
        cursor["deep"] = child
        cursor = child
    cleaned = public_payload("other", nested)
    cursor = cleaned
    for _ in range(DEPTH_LIMIT + 1):
        assert isinstance(cursor, dict)
        cursor = cursor["deep"]
    assert cursor == "\u2026"
    wide = {f"key-{index}": index for index in range(KEY_LIMIT + 5)}
    assert len(public_payload("other", wide)) == KEY_LIMIT
    long_list = list(range(LIST_LIMIT + 5))
    cleaned_list = public_payload("other", long_list)["value"]
    assert len(cleaned_list) == LIST_LIMIT
