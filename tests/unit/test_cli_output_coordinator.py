"""Output failure must not strand terminal fences or session shutdown."""

import asyncio
from types import SimpleNamespace

import pytest

from redteam.app.cli.output_coordinator import OutputCoordinator
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult, SessionPhase

pytestmark = pytest.mark.asyncio


class FailingFrontend:
    interactive = True

    def __init__(self, *, fatal=True, blocked=False):
        self.fatal = fatal
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.fallback = []
        self.closed = False

    def bind(self, controller):
        self.controller = controller

    def invalidate(self):
        pass

    async def run(self):
        await self.controller.closing.wait()

    def close(self):
        self.closed = True

    async def write(self, content, console):
        self.entered.set()
        await self.release.wait()
        raise OSError("output unavailable")

    def emergency(self, text):
        if self.fatal:
            raise OSError("emergency output unavailable")
        self.fallback.append(text)


async def close_failed(output):
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(output.close(), 1)
    assert output.task.done()


async def test_write_failure_keeps_working_emergency_output():
    frontend = FailingFrontend(fatal=False)
    output = OutputCoordinator(None, frontend)
    output.start()
    output.emit("first")
    await asyncio.wait_for(output.fence(), 1)
    output.emit("second")
    await asyncio.wait_for(output.close(), 1)
    assert "first" in frontend.fallback
    assert "second" in frontend.fallback


async def test_fence_detects_consumer_already_dead():
    frontend = FailingFrontend()
    output = OutputCoordinator(None, frontend)
    output.start()
    output.emit("first")
    await frontend.entered.wait()
    await asyncio.wait({output.task}, timeout=1)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(output.fence(), 1)
    await close_failed(output)


@pytest.mark.parametrize("capacity", [1, 4])
async def test_waiting_fence_unblocks_on_consumer_death(capacity):
    frontend = FailingFrontend(blocked=True)
    output = OutputCoordinator(None, frontend, capacity=capacity)
    output.start()
    output.emit("first")
    await frontend.entered.wait()
    output.emit("queued")
    pending = asyncio.create_task(output.fence())
    await asyncio.sleep(0)
    assert not pending.done()
    frontend.release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(pending, 1)
    await close_failed(output)


async def test_cancelled_fence_does_not_cancel_consumer():
    frontend = FailingFrontend(fatal=False, blocked=True)
    output = OutputCoordinator(None, frontend, capacity=1)
    output.start()
    output.emit("first")
    await frontend.entered.wait()
    output.emit("queued")
    pending = asyncio.create_task(output.fence())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not output.task.done()
    frontend.release.set()
    await asyncio.wait_for(output.close(), 1)


async def test_overload_emergency_failure_reaches_fence():
    frontend = FailingFrontend(blocked=True)
    output = OutputCoordinator(None, frontend, capacity=1)
    output.start()
    output.emit("first")
    await frontend.entered.wait()
    output.emit("queued")
    output.emit("overflow")
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(output.fence(), 1)
    await close_failed(output)


async def test_full_queue_keeps_coalesced_notice():
    frontend = FailingFrontend(fatal=False, blocked=True)
    output = OutputCoordinator(None, frontend, capacity=1)
    output.start()
    output.emit("first")
    await frontend.entered.wait()
    output.emit("queued")
    output.emit("progress", critical=False)
    pending = asyncio.create_task(output.fence())
    await asyncio.sleep(0)
    frontend.release.set()
    await asyncio.wait_for(pending, 1)
    assert any("진행 알림 1개 집계됨" in text for text in frontend.fallback)
    await asyncio.wait_for(output.close(), 1)


async def test_cancelled_close_cleans_up_consumer():
    frontend = FailingFrontend(blocked=True)
    output = OutputCoordinator(None, frontend)
    output.start()
    output.emit("first")
    await frontend.entered.wait()
    pending = asyncio.create_task(output.close())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)
    assert output.task.done()


async def test_session_closes_on_startup_output_failure():
    frontend = FailingFrontend()
    calls = []

    async def runner(*args):
        calls.append(args)
        return RunResult()

    controller = SessionController(
        console=None,
        frontend=frontend,
        conversation=None,
        runner=runner,
        engagement_name="test",
        scope=["localhost"],
    )
    result = await asyncio.wait_for(controller.run(), 1)
    assert result.exit_code == 1
    assert result.error
    assert controller.phase == SessionPhase.CLOSED
    assert frontend.closed
    assert not calls
    assert all(task.done() for task in controller.workers)


async def test_final_output_failure_preserves_run_and_receipt():
    frontend = FailingFrontend(blocked=True)
    finished = SimpleNamespace(
        run_id="run-a",
        phase=SimpleNamespace(value="finished"),
        termination_reason="mission_completed",
        mission_status=None,
        actions_executed=1,
        recent_observations=[],
    )

    async def runner(objective, controller):
        frontend.release.set()
        return RunResult(final=finished)

    controller = SessionController(
        console=None,
        frontend=frontend,
        conversation=None,
        runner=runner,
        engagement_name="test",
        scope=["localhost"],
        single_run=True,
    )
    task = asyncio.create_task(controller.run())
    await frontend.entered.wait()
    assert controller.submit("/new inspect localhost")
    result = await asyncio.wait_for(task, 1)
    assert result.final is finished
    assert result.exit_code == 1
    assert controller.run_task is None
    assert controller.binding is None
    assert controller.last_binding.run_id == "run-a"
    assert controller.receipts["input_1"].state == "handled"
    assert controller.idle.is_set()
    assert controller.phase == SessionPhase.CLOSED


async def test_fatal_output_during_active_run_skips_deferred():
    frontend = FailingFrontend(blocked=True)
    finished = SimpleNamespace(
        run_id="run-a",
        phase=SimpleNamespace(value="finished"),
        termination_reason="mission_completed",
        mission_status=None,
        actions_executed=1,
        recent_observations=[],
    )
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def runner(objective, controller):
        calls.append(objective)
        started.set()
        await asyncio.wait_for(finish.wait(), 2)
        frontend.release.set()
        return RunResult(final=finished)

    controller = SessionController(
        console=None,
        frontend=frontend,
        conversation=None,
        runner=runner,
        engagement_name="test",
        scope=["localhost"],
    )
    task = asyncio.create_task(controller.run())
    await asyncio.wait_for(frontend.entered.wait(), 2)
    assert controller.submit("/new first localhost")
    await asyncio.wait_for(started.wait(), 2)
    assert controller.submit("/new second localhost")
    await asyncio.wait_for(controller.queue.join(), 2)
    assert controller.receipts["input_2"].state == "deferred"
    finish.set()
    result = await asyncio.wait_for(task, 3)
    assert len(calls) == 1
    assert result.final is finished
    assert result.exit_code == 1
    assert controller.receipts["input_1"].state == "handled"
    assert controller.receipts["input_2"].state == "not_applied"
    assert controller.phase == SessionPhase.CLOSED
