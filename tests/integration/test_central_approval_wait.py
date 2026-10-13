"""Approval polling inspects originals, but never repeatedly drives pending actors."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redteam.app.central_development import build_central_development
from redteam.app.cli.commands import run as run_command
from redteam.core.enums import ApprovalStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_development import engagement, gateway
from tests.integration.test_central_parallel import limits
from tests.integration.test_central_recovery import start


async def test_pending_approval_resumes_only_after_decision(monkeypatch):
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as session:
        original, = await start(session, llm)
        final = original.model_copy(update={
            "termination_reason": TerminationReason.INVESTIGATION_HELD,
        })
        terminal = final.model_copy(update={
            "termination_reason": TerminationReason.OPERATOR_STOPPED,
        })
        calls = []
        async def resume(**kwargs):
            status = await session.deps.control_plane.inspect_approval(original.pending_approval_id)
            assert status.status is ApprovalStatus.APPROVED
            calls.append(kwargs)
            return terminal
        session.resume = resume
        ticks = 0
        sleep = asyncio.sleep
        async def poll(delay):
            nonlocal ticks
            if delay == 0.5:
                ticks += 1
                if ticks == 3:
                    await session.deps.control_plane.resolve_approval(
                        original.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
                    )
                await sleep(0)
            else:
                await sleep(delay)
        monkeypatch.setattr(run_command.asyncio, "sleep", poll)
        channel = SimpleNamespace(suspended=lambda *args: None, resumed=lambda: None)
        result = await run_command.wait_for_central_suspensions(
            session, final, input_live=lambda: True, echo=lambda line: None,
            session_channel=channel,
        )
        assert result is terminal and ticks == 3 and len(calls) == 1
        assert tools.invocations == []


async def test_recovery_error_is_shown_once_and_stops_automatic_retry():
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as session:
        original, = await start(session, llm)
        await session.deps.control_plane.resolve_approval(
            original.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        final = original.model_copy(update={
            "termination_reason": TerminationReason.INVESTIGATION_HELD,
        })
        held = final.model_copy(update={"planning_diagnostic": DecisionDiagnostic(
            code="central_execution_recovery_held", stage="execution_reconciliation",
            kind="validation", summary="Original record missing.", retryable=False,
        )})
        session.resume = AsyncMock(return_value=held)
        displayed = []
        channel = SimpleNamespace(suspended=lambda *args: None, resumed=lambda: None)
        result = await run_command.wait_for_central_suspensions(
            session, final, input_live=lambda: True, echo=displayed.append, session_channel=channel,
        )
        assert result is held and session.resume.await_count == 1
        assert any("central_execution_recovery_held" in message for message in displayed)
        assert tools.invocations == []
