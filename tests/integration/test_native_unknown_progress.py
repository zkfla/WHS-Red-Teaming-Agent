"""Native request accounting for immutable central execution outcomes."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from redteam.app.central_development import build_central_development
from redteam.capability.development import DevelopmentResponse
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.tool import ToolError
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ready
from redteam.orchestration.nodes.react import append_completed_results
from redteam.orchestration.state import NativeCallState, TerminationReason
from tests.integration.test_central_development import (
    TOOL,
    discover,
    engagement,
    gateway,
    inspect,
    register,
    settings,
    turn,
)


async def _drive(session):
    worker = await ready(session.deps, session.runtime)
    assert worker is not None
    outcome = None
    async for outcome in session.driver.drive([worker]):
        assert outcome.kind == "completed", outcome.error
    assert outcome is not None
    return outcome.runtime


def _request_row(state, request_id):
    with closing(sqlite3.connect(state.path)) as db:
        row = db.execute(
            "SELECT payload FROM state_requests WHERE identity=? AND kind='llm_request'",
            (request_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _request_rows(state, run_id):
    with closing(sqlite3.connect(state.path)) as db:
        rows = db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? AND kind='llm_request'",
            (run_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


async def test_unknown_receipt_accounts_no_progress_without_closing_or_replaying(tmp_path):
    tools = gateway(unknown=True, resource=True)
    llm = FakeLLMClient(native_responses=[discover(TOOL), turn(inspect("uncertain"))])
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(),
    ) as session:
        task_id, = await register(session, "unknown-progress")
        runtime = await _drive(session)
        assert runtime.termination_reason == TerminationReason.RESOURCE_WAIT

        runner = session.driver.runner_for(runtime)
        snapshot = await runner._graph.aget_state(runner.config())
        call_state, = snapshot.values["native_calls"]
        assert call_state.completed is False
        assert call_state.made_progress is False
        assert call_state.decision_valid is True
        assert call_state.decision_request_id

        execution, = await session.deps.central_state.list_executions()
        assert str(execution.task_id) == str(task_id)
        assert execution.state is ExecutionState.UNKNOWN
        assert execution.resource_execution_pending is True
        assert len(tools.invocations) == 1

        row = _request_row(session.deps.central_state, call_state.decision_request_id)
        assert row is not None and row["made_progress"] is False
        usage = await session.deps.central_state.request_ledger().snapshot(str(runtime.run_id))
        assert usage["no_progress"] >= 1

        resumed = await runner.resume_recovered(runtime)
        assert resumed.termination_reason == TerminationReason.RESOURCE_WAIT
        assert len(tools.invocations) == 1
        row_after_resume = _request_row(
            session.deps.central_state, call_state.decision_request_id,
        )
        assert row_after_resume is not None and row_after_resume["made_progress"] is False


async def test_unknown_and_useful_sibling_settle_one_request_as_progress(tmp_path):
    tools = gateway(resource=True)
    tools.responses = [
        DevelopmentResponse(output={"status": "observed"}),
        DevelopmentResponse(output={"status": "uncertain"}, metadata={
            "execution_uncertain": True,
        }),
    ]
    llm = FakeLLMClient(native_responses=[
        discover(TOOL),
        turn(inspect("useful-first"), inspect("unknown-second")),
    ])
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(),
    ) as session:
        task_id, = await register(session, "mixed-unknown-progress")
        runtime = await _drive(session)
        assert runtime.termination_reason == TerminationReason.RESOURCE_WAIT

        runner = session.driver.runner_for(runtime)
        snapshot = await runner._graph.aget_state(runner.config())
        calls = snapshot.values["native_calls"]
        assert [call_state.completed for call_state in calls] == [True, False]
        assert [call_state.made_progress for call_state in calls] == [True, False]
        executions = await session.deps.central_state.list_executions(task_id=str(task_id))
        assert len(executions) == 2
        request_ids = {call_state.decision_request_id for call_state in calls}
        assert len(request_ids) == 1
        row = _request_row(session.deps.central_state, request_ids.pop())
        assert row is not None and row["made_progress"] is True
        assert len(tools.invocations) == 2


async def test_unknown_first_keeps_unattempted_valid_sibling_pending(tmp_path):
    tools = gateway(resource=True)
    tools.responses[0] = DevelopmentResponse(output={"status": "uncertain"}, metadata={
        "execution_uncertain": True,
    })
    llm = FakeLLMClient(native_responses=[
        discover(TOOL),
        turn(inspect("unknown-first"), inspect("useful-later")),
    ])
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(),
    ) as session:
        await register(session, "unknown-first-progress")
        runtime = await _drive(session)
        runner = session.driver.runner_for(runtime)
        snapshot = await runner._graph.aget_state(runner.config())
        calls = snapshot.values["native_calls"]
        assert [call_state.completed for call_state in calls] == [False, False]
        assert [call_state.made_progress for call_state in calls] == [False, None]
        assert calls[1].dispatch is not None
        assert len(tools.invocations) == 1

        row = _request_row(session.deps.central_state, calls[0].decision_request_id)
        assert row is not None and row.get("made_progress") is None
        resumed = await runner.resume_recovered(runtime)
        assert resumed.termination_reason == TerminationReason.RESOURCE_WAIT
        assert len(tools.invocations) == 1
        row_after_resume = _request_row(
            session.deps.central_state, calls[0].decision_request_id,
        )
        assert row_after_resume is not None and row_after_resume.get("made_progress") is None


async def test_failed_dispatch_is_no_progress(tmp_path):
    tools = gateway()
    tools.responses[0] = DevelopmentResponse(
        success=False,
        error=ToolError(code="synthetic_failure", message="Synthetic failure"),
    )
    llm = FakeLLMClient(native_responses=[discover(TOOL), turn(inspect("failed"))])
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(),
    ) as session:
        await register(session, "failed-progress")
        runtime = await _drive(session)
        tool_rows = [row for row in _request_rows(
            session.deps.central_state, str(runtime.run_id),
        ) if row.get("purpose") == "worker_external_tool_decision"]
        assert len(tool_rows) == 1
        assert tool_rows[0]["made_progress"] is False


async def test_progress_settlement_isolated_by_originating_request_id():
    class Ledger:
        def __init__(self):
            self.settled = []

        async def mark_progress(self, run_id, request_id, *, made_progress):
            self.settled.append((run_id, request_id, made_progress))

    ledger = Ledger()
    deps = SimpleNamespace(central_state=SimpleNamespace(request_ledger=lambda: ledger))
    state = {
        "runtime": SimpleNamespace(run_id="run-native-progress"),
        "native_calls": [
            NativeCallState(
                call_id="failed", name="external", tool_id="external.tool",
                decision_request_id="request-failed", decision_valid=True,
                made_progress=False,
            ),
            NativeCallState(
                call_id="useful", name="external", tool_id="external.tool",
                decision_request_id="request-useful", decision_valid=True,
                made_progress=True,
            ),
        ],
    }

    await append_completed_results(state, deps=deps)
    assert ledger.settled == [
        ("run-native-progress", "request-failed", False),
        ("run-native-progress", "request-useful", True),
    ]
