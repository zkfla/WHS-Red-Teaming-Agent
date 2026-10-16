"""Probe physical-action cap leaves local completion available while model budget remains.

Real native probe loop, real tool.get_detail discovery, real evidence.read and task.settle.
Only the model turns are scripted and the target is the existing synthetic inspection double.
Each scripted turn reads the newest tool feedback, so the next turn cites the committed original.
"""

import json
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.events import EventType
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import process_controls
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.state import TerminationReason
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TOOL, call, discover, gateway, inspect, turn
from tests.integration.test_probe_workers import drive, register, selection

ACTION_CAP = "probe_action_limit_exhausted"


class LocalCompletionLLM(FakeLLMClient):
    """Scripted worker turns; each script step receives the messages of that request."""

    def __init__(self, script):
        super().__init__()
        self.worker = None
        self.script = script
        self.model_calls = []

    async def complete_native(self, messages, tools, system=None, **kwargs):
        context = worker_input(messages)
        if (context.get("active_task") or {}).get("task_id") != self.worker:
            return NativeCompletion(text="No action for this purpose.")
        self.model_calls.append(messages)
        return await self.script(len(self.model_calls) - 1, messages)


async def test_action_cap_still_lets_model_read_original_and_settle_locally(tmp_path):
    observed = {}

    async def script(step, messages):
        if step == 0:
            return discover(TOOL, "evidence.read", "task.settle")
        if step == 1:
            # The second physical call in the same batch must be denied before invocation.
            return turn(inspect("observe", "a"), inspect("hostile", "b"))
        if step == 2:
            observed["messages"] = messages
            evidence_id = _evidence_of_original(messages)
            return turn(call("read-original", "evidence.read", {"evidence_id": evidence_id}))
        if step == 3:
            evidence_id = _evidence_of_original(messages)
            return turn(call("settle", "task.settle", {
                "outcome": "inconclusive",
                "reason": "The preserved original was read and needs no further target request",
                "evidence_ids": [evidence_id],
            }))
        return NativeCompletion(text="Settled within the remaining model budget.")

    llm = LocalCompletionLLM(script)
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identity = await register(session, "local-after-cap", actions=1, requests=4)
        llm.worker = identity
        result, = await drive(session)
        assert result.kind == "completed", result.error

        # One target request only: the cap denies the second call before any invocation.
        assert len(tools.inner.invocations) == 1
        assert len(llm.model_calls) == 4
        denied = [m for m in observed["messages"] if m.get("role") == "tool"
                  and ACTION_CAP in m["content"]]
        assert denied

        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED
        assert task.result.outcome == "inconclusive"
        original = _evidence_of_original(observed["messages"])
        assert original in set(map(str, task.result.evidence_ids))
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["actions"] == 1 and usage["model_requests"] == 4
        # Replan at the cap adds no execution: the original is the only physical row.
        assert len(await session.deps.central_state.list_executions()) == 1


async def test_zero_model_headroom_yields_immediately_after_action_cap(tmp_path):
    async def script(step, messages):
        if step == 0:
            return discover(TOOL)
        if step == 1:
            return turn(inspect("observe", "a"))
        raise AssertionError("no model request may follow exhausted model headroom")

    llm = LocalCompletionLLM(script)
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identity = await register(session, "zero-headroom", actions=1, requests=2)
        llm.worker = identity
        result, = await drive(session)
        assert result.kind == "completed", result.error
        assert len(llm.model_calls) == 2
        assert len(tools.inner.invocations) == 1
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED and task.result.outcome == "inconclusive"
        assert "probe_model_request_limit_exhausted" in task.result.limitations


@pytest.mark.parametrize("mode", ["approval", "unknown"])
async def test_pending_original_at_action_cap_gets_no_local_turn(tmp_path, mode):
    tools = OriginalProtocolDouble(gateway(risk="high" if mode == "approval" else "low",
                                          unknown=mode == "unknown"))

    async def script(step, messages):
        if step == 0:
            return discover(TOOL)
        if step == 1:
            return turn(inspect("pending-original", "a"))
        raise AssertionError("a pending original must not open a local model turn")

    llm = LocalCompletionLLM(script)
    async with selection(tmp_path, tools=tools, llm=llm) as (session, llm, tools):
        identity = await register(session, "pending-cap", actions=1, requests=4)
        llm.worker = identity
        result, = await drive(session)
        assert result.kind == "completed", result.error
        assert len(llm.model_calls) == 2
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.BLOCKED and task.status is not TaskStatus.COMPLETED
        original, = await session.deps.central_state.list_executions()
        assert original.state in {ExecutionState.PENDING, ExecutionState.UNKNOWN}


@pytest.mark.parametrize(("status", "reason"), [
    (MissionStatus.PAUSED, TerminationReason.OPERATOR_PAUSED),
    (MissionStatus.CANCELLED, TerminationReason.OPERATOR_STOPPED),
])
async def test_operator_stop_after_original_keeps_local_turn_closed(tmp_path, status, reason):
    async def script(step, messages):
        if step == 0:
            return discover(TOOL, "task.settle")
        if step == 1:
            return turn(inspect("observe", "a"))
        raise AssertionError("an operator stop must not open a local model turn")

    llm = LocalCompletionLLM(script)
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identity = await register(session, "stopped-cap", actions=1, requests=4)
        llm.worker = identity
        subscribed = []

        async def stop_after_original(envelope):
            if subscribed:
                return
            subscribed.append(envelope)

            def update(tx):
                mission = tx.get_mission(tx.binding()[0])
                tx.update_mission(mission.model_copy(update={
                    "status": status, "revision": mission.revision + 1,
                }), expected_revision=mission.revision)

            await session.deps.central_state.transaction(update)

        await session.deps.event_bus.subscribe(EventType.EVIDENCE_CREATED, stop_after_original)
        result, = await drive(session)
        assert result.kind == "completed", result.error
        assert len(tools.inner.invocations) == 1
        assert len(llm.model_calls) == 2
        assert result.runtime.termination_reason is reason
        task = await session.deps.central_state.get_task(identity)
        assert task.status is not TaskStatus.COMPLETED


async def _operator_intent(session, kind):
    """Canonical operator intent path: append the intent, then the existing control processor."""
    await session.deps.central_state.append(IntentRecord(
        intent=OperatorIntent(
            intent_id="operator-" + kind, mission_id=session.runtime.mission_id,
            intent_type=kind, instruction=kind, created_at=datetime.now(timezone.utc),
            provenance=Provenance(source="fixture:operator"),
        ), run_id=session.runtime.run_id, correlation_id="operator-" + kind,
    ))
    await process_controls(session.deps)


async def test_pause_then_resume_at_cap_takes_one_local_turn_without_second_physical(tmp_path):
    async def script(step, messages):
        if step == 0:
            return discover(TOOL, "evidence.read", "task.settle")
        if step == 1:
            return turn(inspect("observe", "a"))
        if step == 2:
            evidence_id = _evidence_of_original(messages)
            return turn(call("read-original", "evidence.read", {"evidence_id": evidence_id}))
        if step == 3:
            evidence_id = _evidence_of_original(messages)
            return turn(call("settle", "task.settle", {
                "outcome": "inconclusive",
                "reason": "The preserved original was read after the operator resumed",
                "evidence_ids": [evidence_id],
            }))
        return NativeCompletion(text="Settled within the remaining model budget.")

    llm = LocalCompletionLLM(script)
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identity = await register(session, "pause-resume-cap", actions=1, requests=4)
        llm.worker = identity
        paused = []

        async def pause_after_original(envelope):
            if paused:
                return
            paused.append(envelope)
            await _operator_intent(session, "pause")

        await session.deps.event_bus.subscribe(EventType.EVIDENCE_CREATED, pause_after_original)
        result, = await drive(session)
        assert result.runtime.termination_reason is TerminationReason.OPERATOR_PAUSED
        assert len(tools.inner.invocations) == 1 and len(llm.model_calls) == 2
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.PAUSED

        await _operator_intent(session, "resume")
        recovered, outcome = await session.driver.recover_central_instance(result.runtime)
        assert outcome.ready, outcome.errors
        await session.driver.runner_for(recovered).resume_recovered(recovered)

        # The resumed turn settles from the preserved original; no second physical call runs.
        assert len(tools.inner.invocations) == 1
        assert len(llm.model_calls) == 4
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED and task.result.outcome == "inconclusive"
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["actions"] == 1 and usage["model_requests"] == 4


def _evidence_of_original(messages):
    """The evidence ID of the newest executed physical result visible to the model."""
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        body = json.loads(message.get("content") or "{}")
        if body.get("executed") is True and body.get("evidence_ids"):
            return body["evidence_ids"][0]
    raise AssertionError("no executed original is visible to the model")
