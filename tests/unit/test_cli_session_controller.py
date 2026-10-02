"""The plan's boundary scenarios: routing, settle races, uncertain writes, controls."""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from rich.console import Console

from redteam.app.cli.conversation import ConversationDecision
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult, SessionPhase
from redteam.app.cli.terminal_frontend import ConsoleFrontend

pytestmark = pytest.mark.asyncio


class Frontend(ConsoleFrontend):
    interactive = True


class Conversation:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.mode = "answer"
        self.calls = []

    async def decide(self, text, **kwargs):
        self.calls.append((text, kwargs))
        self.entered.set()
        if self.block:
            await self.release.wait()
        return ConversationDecision(
            mode=self.mode,
            response="공개 답변",
            objective=text,
            guidance=text,
            investigation="new" if self.mode == "execute" else None,
        )


class Operator:
    def __init__(self, end):
        self._identity = SimpleNamespace(mission_id="mission-A")
        self.end = end
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.uncertain = False

    async def session_context(self):
        return "현재 A 조사"

    async def dispatch(self, kind, value):
        self.calls.append((kind, value))
        self.entered.set()
        if kind == "stop":
            self.end.set()
        if self.block:
            await self.release.wait()
        if self.uncertain:
            raise OSError("response lost after commit")
        return True, "수락됨"


@asynccontextmanager
async def session():
    buffer = io.StringIO()
    console = Console(file=buffer, width=80, color_system=None)
    conversation = Conversation()
    started, end = asyncio.Event(), asyncio.Event()
    operator = Operator(end)
    calls = []

    async def runner(objective, controller):
        calls.append(objective)
        controller.attach(operator)
        controller.observe(SimpleNamespace(run_id="run-A"))
        started.set()
        await end.wait()
        final = SimpleNamespace(
            run_id="run-A",
            phase=SimpleNamespace(value="finished"),
            termination_reason="no_actionable_plan",
            mission_status=None,
            actions_executed=1,
            recent_observations=[],
        )
        return RunResult(final)

    c = SessionController(
        console=console,
        frontend=Frontend(console),
        conversation=conversation,
        runner=runner,
        engagement_name="test",
        scope=["localhost"],
    )
    c.output.start()
    c.workers = [
        asyncio.create_task(c._consume(c.queue)),
        asyncio.create_task(c._consume(c.controls)),
    ]
    try:
        yield SimpleNamespace(
            c=c,
            conversation=conversation,
            operator=operator,
            calls=calls,
            started=started,
            end=end,
            buffer=buffer,
        )
    finally:
        conversation.release.set()
        operator.release.set()
        end.set()
        await c.close()


async def start(s):
    s.c.submit("/new localhost 조사")
    await asyncio.wait_for(s.started.wait(), 2)


async def test_routing_questions_guidance_and_new_investigation_have_distinct_effects():
    async with session() as s:
        await start(s)
        s.c.submit("무엇을 찾았어?")
        await s.c.queue.join()
        assert not s.operator.calls
        s.conversation.mode = "guidance"
        s.c.submit("로그인을 더 봐줘")
        await s.c.queue.join()
        assert s.operator.calls == [("guidance", "로그인을 더 봐줘")]
        s.conversation.mode = "execute"
        s.c.submit("별도로 localhost 조사")
        await s.c.queue.join()
        assert len(s.calls) == 1
        assert len(s.c.deferred) == 1
        assert len(s.operator.calls) == 1


@pytest.mark.parametrize("mode", ["answer", "guidance"])
async def test_slow_classification_keeps_original_binding_after_run_ends(mode):
    async with session() as s:
        await start(s)
        old_binding = s.c.input_binding
        s.conversation.mode = mode
        s.conversation.block = True
        s.c.submit("A에 대한 입력", binding=old_binding)
        await s.conversation.entered.wait()
        s.end.set()
        await s.c.run_task
        s.conversation.release.set()
        await s.c.queue.join()
        assert not s.operator.calls
        assert len(s.calls) == 1
        receipt = s.c.receipts["input_2"]
        assert receipt.envelope.binding == old_binding
        assert receipt.state == ("handled" if mode == "answer" else "not_applied")


async def test_inflight_write_survives_completion_without_retry_on_uncertain_response():
    async with session() as s:
        await start(s)
        s.conversation.mode = "guidance"
        s.operator.block = s.operator.uncertain = True
        s.c.submit("현재 지시")
        await s.operator.entered.wait()
        running = s.c.run_task
        s.end.set()
        s.operator.release.set()
        await s.c.queue.join()
        await running
        assert s.c.receipts["input_2"].state == "unknown"
        assert s.operator.calls == [("guidance", "현재 지시")]
        assert not s.c.in_flight


async def test_controls_bypass_chat_and_stale_approval_does_not_resolve_next_request():
    async with session() as s:
        await start(s)
        s.conversation.block = True
        s.c.submit("오래 걸리는 질문")
        await s.conversation.entered.wait()
        s.c.pending_approval = "approval-A"
        s.c.set_phase(SessionPhase.WAITING_APPROVAL)
        s.c.submit("approve", approval_id="approval-old")
        await s.c.controls.join()
        assert not s.operator.calls
        s.c.submit("approve", approval_id="approval-A")
        await s.c.controls.join()
        assert s.operator.calls == [("approve", "approval-A")]
        s.c.resumed()
        assert s.c.phase == SessionPhase.RUNNING
        s.c.submit("stop")
        await s.c.controls.join()
        assert s.operator.calls[-1] == ("stop", "")
        assert not s.conversation.release.is_set()


async def test_failure_result_once_and_session_accepts_next_question():
    async with session() as s:

        async def fail(objective, controller):
            raise RuntimeError("bootstrap unavailable")

        s.c.runner = fail
        s.c.submit("/new test")
        await s.c.join_inputs()
        assert s.c.phase == SessionPhase.IDLE
        assert s.c.final_result.exit_code == 1
        s.c.submit("왜 실패했어?")
        await s.c.queue.join()
        await s.c.output.fence()
        assert s.buffer.getvalue().count("실행 실패 · RunId:") == 1
        assert s.c.receipts["input_2"].state == "handled"
        assert "bootstrap unavailable" in s.conversation.calls[-1][1]["session_context"]


async def test_suspension_and_resume_share_run_but_not_attachment():
    async with session() as s:
        checkpoint = SimpleNamespace(
            run_id="same-run",
            phase=SimpleNamespace(value="replan"),
            termination_reason="approval_pending",
            pending_approval_id="approval-A",
            mission_status=None,
            actions_executed=0,
            recent_observations=[],
        )

        async def suspended(objective, controller):
            return RunResult(checkpoint)

        resumed = []

        async def resume(kind, value, run_id, controller):
            resumed.append((kind, value, run_id))
            return RunResult(
                SimpleNamespace(
                    run_id=run_id,
                    phase=SimpleNamespace(value="finished"),
                    termination_reason="no_actionable_plan",
                    pending_approval_id=None,
                    mission_status=None,
                    actions_executed=1,
                    recent_observations=[],
                )
            )

        s.c.runner, s.c.resume_runner = suspended, resume
        s.c.submit("/new test")
        await s.c.join_inputs()
        first = s.c.last_binding
        assert s.c.phase == SessionPhase.WAITING_APPROVAL
        s.c.submit("approve approval-A")
        await s.c.join_inputs()
        assert resumed == [("approve", "approval-A", "same-run")]
        assert s.c.last_binding.run_id == first.run_id
        assert s.c.last_binding.attachment_id != first.attachment_id
        assert s.c.phase == SessionPhase.IDLE
        await s.c.output.fence()
        assert "실행 대기" in s.buffer.getvalue()
        assert "실행 종료" in s.buffer.getvalue()


class _ScriptFrontend(ConsoleFrontend):
    """C1: sequential reader with scripted termination (boom/eof/hang)."""

    interactive = False

    def __init__(self, console, behavior="hang", close_error=None):
        super().__init__(console)
        self.behavior = behavior
        self.close_error = close_error
        self.closed = False
        self.run_finished = asyncio.Event()

    async def run(self):
        try:
            if self.behavior == "boom":
                raise OSError("input gone")
            if self.behavior == "boom_after_run":
                while self.controller.run_task is None:
                    await asyncio.sleep(0.01)
                raise OSError("input gone")
            if self.behavior == "eof":
                self.controller.output.emit("Session ended. 세션 종료 요청됨.")
                self.controller.request_close()
                return
            await self.controller.closing.wait()
        finally:
            self.run_finished.set()

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _run_controller(frontend):
    buffer = io.StringIO()
    console = Console(file=buffer, width=80, color_system=None)

    async def runner(objective, controller):
        return RunResult()

    return SessionController(
        console=console,
        frontend=frontend,
        conversation=Conversation(),
        runner=runner,
        engagement_name="test",
        scope=["localhost"],
    )


async def test_sequential_input_failure_records_error_and_closes():
    frontend = _ScriptFrontend(Console(file=io.StringIO(), width=80), behavior="boom")
    c = _run_controller(frontend)
    result = await asyncio.wait_for(c.run(), 10)
    assert result.exit_code == 1
    assert "CLI 입력 실패(OSError)" in (result.error or "")
    assert c.phase == SessionPhase.CLOSED
    assert frontend.run_finished.is_set()


async def test_frontend_close_failure_still_reaps_reader():
    frontend = _ScriptFrontend(
        Console(file=io.StringIO(), width=80),
        behavior="hang",
        close_error=RuntimeError("tui gone"),
    )
    c = _run_controller(frontend)
    run_task = asyncio.create_task(c.run())
    await asyncio.sleep(0.2)
    c.submit("exit")
    result = await asyncio.wait_for(run_task, 10)
    assert result.exit_code == 1
    assert "CLI 화면 정리 실패" in (result.error or "")
    assert c.phase == SessionPhase.CLOSED
    assert frontend.closed
    assert frontend.run_finished.is_set()


async def test_clean_reader_return_keeps_clean_result():
    frontend = _ScriptFrontend(Console(file=io.StringIO(), width=80), behavior="eof")
    c = _run_controller(frontend)
    result = await asyncio.wait_for(c.run(), 10)
    assert result.exit_code == 0
    assert result.error is None
    assert c.phase == SessionPhase.CLOSED


async def test_input_failure_survives_clean_run_finish_during_close():
    frontend = _ScriptFrontend(
        Console(file=io.StringIO(), width=80), behavior="boom_after_run"
    )
    c = _run_controller(frontend)
    release = asyncio.Event()

    async def blocking_runner(objective, controller):
        await release.wait()
        return RunResult()

    c.runner = blocking_runner
    run_task = asyncio.create_task(c.run())
    await asyncio.sleep(0.2)
    c.submit("/new localhost 조사")
    await asyncio.wait_for(c.closing.wait(), 10)
    release.set()
    result = await asyncio.wait_for(run_task, 10)
    assert result.exit_code == 1
    assert "CLI 입력 실패(OSError)" in (result.error or "")
    assert c.phase == SessionPhase.CLOSED
