"""Safe conversation diagnostics reach the error, the input receipt, and CLI notices."""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from redteam.app.cli.commands import interactive as interactive_command
from redteam.app.cli.commands import run as run_command
from redteam.app.cli.conversation import (
    ConversationAssistant,
    ConversationError,
    conversation_failure_label,
)
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult
from redteam.app.cli.terminal_frontend import ConsoleFrontend
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import MissionStatus
from redteam.core.ids import EngagementId, MissionId, RunId
from redteam.core.ports.llm import LLMProviderFailure
from redteam.event_bus import InMemoryEventBus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.operator_intents.models import IntentExplanation, IntentStatusReport
from redteam.orchestration.operator_intents.service import OperatorIntentService
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes.control_plane import FakeControlPlane
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.fakes.tool_gateway import DummyToolGateway

SECRET = "sk-live-conversation-diagnostic-marker"
QUESTION = "지금 어떤 상태야?"


class _Hang:
    """Sentinel reply: the provider accepts the request and never answers in time."""


_HANG = _Hang()


class _ScriptedLLM:
    """Explicit scripted provider; every request goes through the structured path."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, _Hang):
            await asyncio.sleep(10)
        if isinstance(item, BaseException):
            raise item
        return item

    async def complete(self, **kwargs: Any) -> str:
        raise AssertionError("conversation must use structured decisions")


class _SlowLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(10)
        return {"mode": "answer", "response": "late"}

    async def complete(self, **kwargs: Any) -> str:
        raise AssertionError("conversation must use structured decisions")


class _Frontend(ConsoleFrontend):
    interactive = True


class _RaisingConversation:
    """Stands in for a conversation object whose decide fails outside the client."""

    async def decide(self, text: str, **kwargs: Any) -> Any:
        raise RuntimeError(f"raw provider body {SECRET}")


class _FakeIntentService(OperatorIntentService):
    """Public status/explain surface; the store-backed constructor is not run."""

    def __init__(self) -> None:
        pass

    async def status(self, *, mission_id: MissionId) -> IntentStatusReport:
        return IntentStatusReport(mission_id=mission_id, mission_status=MissionStatus.ACTIVE)

    async def explain(self, *, mission_id: MissionId, run_id: RunId) -> IntentExplanation:
        return IntentExplanation(
            mission_id=mission_id, summary="explain", mission_status=MissionStatus.ACTIVE
        )


def _rate_limited() -> LLMProviderFailure:
    return LLMProviderFailure(
        kind="rate_limited", http_status=429, provider_code="rate_limited", retryable=True,
    )


async def _decide_error(assistant: ConversationAssistant) -> ConversationError:
    with pytest.raises(ConversationError) as caught:
        await assistant.decide(QUESTION, engagement_name="local", scope=["localhost"])
    return caught.value


@pytest.mark.asyncio
async def test_provider_status_survives_into_conversation_error() -> None:
    llm = _ScriptedLLM([_rate_limited()])

    error = await _decide_error(ConversationAssistant(llm))

    diagnostic = error.diagnostic
    assert isinstance(diagnostic, DecisionDiagnostic)
    assert diagnostic.stage == "conversation_decide"
    assert diagnostic.attempts == len(llm.calls)
    assert diagnostic.last_cause is not None
    assert diagnostic.last_cause.kind == "transport"
    assert diagnostic.last_cause.provider_status == 429
    assert isinstance(error.__cause__, DecisionFailure)
    assert error.__cause__.diagnostic is diagnostic
    label = conversation_failure_label(diagnostic)
    assert "conversation_decide" in label
    assert "status 429" in label
    assert SECRET not in diagnostic.model_dump_json()


@pytest.mark.asyncio
async def test_schema_rule_and_location_survive_budget_exhaustion() -> None:
    llm = _ScriptedLLM([{"mode": "maybe", "response": "reply"}])

    error = await _decide_error(ConversationAssistant(llm))

    diagnostic = error.diagnostic
    assert diagnostic is not None and diagnostic.last_cause is not None
    cause = diagnostic.last_cause
    assert cause.code == "schema_enum"
    assert cause.location == "$.mode"
    assert {"rule": "enum", "location": "$.mode"} in cause.validation_issues
    label = conversation_failure_label(diagnostic)
    assert "rule enum" in label
    assert "at $.mode" in label


@pytest.mark.asyncio
async def test_timeout_is_a_safe_transport_diagnostic_with_its_cause() -> None:
    llm = _SlowLLM()
    started = time.monotonic()
    error = await _decide_error(ConversationAssistant(llm, timeout_seconds=0.05))
    assert time.monotonic() - started < 2.0  # the whole-decision deadline holds

    diagnostic = error.diagnostic
    assert diagnostic is not None
    assert "timed out" in str(error)
    assert diagnostic.stage == "conversation_decide"
    assert diagnostic.retryable is False
    assert llm.calls == 1
    assert diagnostic.attempts == 1
    assert diagnostic.stage_attempts == {"conversation_decide": 1}
    assert diagnostic.normal_used == 1
    assert diagnostic.repair_used == 0
    assert diagnostic.transport_used == 0
    # The outer timeout and the budget deadline race by design; each path keeps its own code.
    if diagnostic.code == "conversation_timeout":
        assert diagnostic.kind == "transport"
        assert diagnostic.error_type == "TimeoutError"
        assert isinstance(error.__cause__, asyncio.TimeoutError)
    else:
        assert diagnostic.code == "decision_time_budget_exhausted"
        assert diagnostic.kind == "budget"
        assert isinstance(error.__cause__, DecisionFailure)
    assert SECRET not in diagnostic.model_dump_json()


@pytest.mark.asyncio
async def test_schema_then_blocked_provider_reports_the_actual_budget() -> None:
    llm = _ScriptedLLM([{"mode": "maybe", "response": "reply"}, _HANG])
    started = time.monotonic()
    error = await _decide_error(ConversationAssistant(llm, timeout_seconds=0.05))
    assert time.monotonic() - started < 2.0

    diagnostic = error.diagnostic
    assert diagnostic is not None
    assert "timed out" in str(error)
    assert len(llm.calls) == 2
    assert diagnostic.attempts == 2
    assert diagnostic.stage_attempts == {"conversation_decide": 2}
    assert diagnostic.normal_used == 1
    assert diagnostic.repair_used == 1
    assert diagnostic.transport_used == 0
    assert diagnostic.failure_counts.get("schema_enum") == 1
    rejections = [row for row in diagnostic.trace if row.get("outcome") == "response_rejected"]
    assert any(
        {"rule": "enum", "location": "$.mode"} in row.get("validation_issues", [])
        for row in rejections
    )
    if diagnostic.code == "conversation_timeout":
        # The outer deadline fired while the repair request was in flight, so the
        # budget's last failure is still the schema rejection.
        assert diagnostic.last_cause is not None
        assert diagnostic.last_cause.code == "schema_enum"
        assert diagnostic.last_cause.location == "$.mode"
    else:
        # The budget deadline fired first: the attempt timeout is the newest
        # failure, and the schema rejection stays in failure_counts and trace.
        assert diagnostic.code == "decision_time_budget_exhausted"
        assert diagnostic.last_cause is not None
        assert diagnostic.last_cause.code == "llm_request_timeout"
    assert SECRET not in diagnostic.model_dump_json()


@pytest.mark.asyncio
async def test_concurrent_decisions_keep_separate_budgets() -> None:
    llm = _ScriptedLLM([{}])
    assistant = ConversationAssistant(llm)

    first, second = await asyncio.gather(_decide_error(assistant), _decide_error(assistant))

    assert len(llm.calls) == 12
    assert first.diagnostic is not None
    assert second.diagnostic is not None
    assert first.diagnostic.attempts == 6
    assert second.diagnostic.attempts == 6
    assert first.diagnostic.stage_attempts == {"conversation_decide": 6}
    assert second.diagnostic.stage_attempts == {"conversation_decide": 6}


@pytest.mark.asyncio
async def test_unexpected_failure_keeps_only_its_type(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant = ConversationAssistant(_ScriptedLLM([{"mode": "answer", "response": "ok"}]))

    async def leaking_ask(**kwargs: Any) -> Any:
        raise RuntimeError(f"Authorization: Bearer {SECRET}")

    monkeypatch.setattr(assistant._client, "ask", leaking_ask)

    error = await _decide_error(assistant)

    diagnostic = error.diagnostic
    assert diagnostic is not None
    assert diagnostic.code == "conversation_failed"
    assert diagnostic.kind == "unknown"
    assert diagnostic.error_type == "RuntimeError"
    assert diagnostic.exception_chain == ["RuntimeError"]
    assert isinstance(error.__cause__, RuntimeError)
    assert SECRET not in diagnostic.model_dump_json()
    assert SECRET not in str(error)


@pytest.mark.asyncio
async def test_provider_exception_message_never_reaches_the_diagnostic() -> None:
    llm = _ScriptedLLM([RuntimeError(f"api_key={SECRET} raw response body")])

    error = await _decide_error(ConversationAssistant(llm))

    assert error.diagnostic is not None
    dumped = error.diagnostic.model_dump_json()
    assert SECRET not in dumped
    assert "raw response body" not in dumped
    assert SECRET not in str(error)


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_into_a_conversation_error() -> None:
    assistant = ConversationAssistant(_SlowLLM(), timeout_seconds=30)
    task = asyncio.create_task(
        assistant.decide(QUESTION, engagement_name="local", scope=["localhost"])
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def _submit_and_settle(conversation: Any) -> tuple[Any, str, list[str]]:
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, color_system=None)
    runs: list[str] = []

    async def runner(objective: str, controller: SessionController) -> RunResult:
        runs.append(objective)
        return RunResult()

    controller = SessionController(
        console=console,
        frontend=_Frontend(console),
        conversation=conversation,
        runner=runner,
        engagement_name="test",
        scope=["localhost"],
    )
    controller.output.start()
    controller.workers = [
        asyncio.create_task(controller._consume(controller.queue)),
        asyncio.create_task(controller._consume(controller.controls)),
    ]
    try:
        assert controller.submit(QUESTION)
        receipt = controller.receipts["input_1"]
        for _ in range(500):
            if receipt.state == "not_applied":
                break
            await asyncio.sleep(0.01)
        assert receipt.state == "not_applied"
    finally:
        await controller.close()
    return receipt, buffer.getvalue(), runs


@pytest.mark.asyncio
async def test_receipt_carries_the_provider_cause_for_the_original_input() -> None:
    receipt, shown, runs = await _submit_and_settle(
        ConversationAssistant(_ScriptedLLM([_rate_limited()]))
    )

    assert receipt.envelope.text == QUESTION
    assert runs == []
    assert receipt.diagnostic is not None
    assert receipt.diagnostic.last_cause is not None
    assert receipt.diagnostic.last_cause.provider_status == 429
    payload = json.loads(receipt.diagnostic.model_dump_json())
    assert payload["last_cause"]["provider_status"] == 429
    assert "status 429" in receipt.reason
    assert "status 429" in shown


@pytest.mark.asyncio
async def test_unexpected_input_failure_is_typed_and_masked() -> None:
    receipt, shown, runs = await _submit_and_settle(_RaisingConversation())

    assert runs == []
    assert receipt.diagnostic is not None
    assert receipt.diagnostic.code == "input_processing_failed"
    assert receipt.diagnostic.stage == "cli_input"
    assert receipt.diagnostic.error_type == "RuntimeError"
    assert SECRET not in receipt.diagnostic.model_dump_json()
    assert SECRET not in receipt.reason
    assert SECRET not in shown


def test_initial_interpretation_notice_shows_the_safe_cause() -> None:
    cause = DecisionDiagnostic(
        code="llm_transport_failed",
        stage="conversation_decide",
        kind="transport",
        provider_status=429,
        summary="The provider request failed.",
    )
    diagnostic = DecisionDiagnostic(
        code="decision_budget_exhausted",
        stage="conversation_decide",
        kind="budget",
        attempts=6,
        summary="The decision budget was spent.",
        last_cause=cause,
    )
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, color_system=None)

    interactive_command._report_initial_interpretation_failure(
        console, ConversationError("conversation decision failed", diagnostic=diagnostic)
    )

    shown = buffer.getvalue()
    assert "decision_budget_exhausted" in shown
    assert "status 429" in shown
    assert "no action started" in shown
    assert SECRET not in shown


@pytest.mark.asyncio
async def test_operator_chat_failure_keeps_the_cause_and_the_run_continues(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    deps = RuntimeDeps(
        control_plane=FakeControlPlane(),
        tool_gateway=DummyToolGateway(),
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=FakeEvidenceStore(),
        llm_client=FakeLLMClient(),
        central_state=CentralStateStore(
            tmp_path / "state.db", run_id="run_operator", engagement_id="eng_operator"
        ),
        settings=OrchestrationSettings(),
    )
    console = run_command.OperatorConsole(
        deps,
        lines.append,
        identity=run_command.RuntimeIdentity(
            run_id=RunId("run_operator"),
            engagement_id=EngagementId("eng_operator"),
            mission_id=MissionId("mission_operator"),
        ),
        conversation=ConversationAssistant(_ScriptedLLM([_rate_limited()])),
        engagement_name="local",
        scope=["localhost"],
    )
    console._service = _FakeIntentService()

    await console._handle_chat_fallback(QUESTION)

    shown = "\n".join(lines)
    assert "could not interpret that as guidance; the run continues" in shown
    assert "status 429" in shown
    assert SECRET not in shown

