"""Upper N-instance driver: AS READY streaming without a batch barrier.

Fake runners stand in for ``AgentLoopRunner`` (whose graph needs full
deps); what is proven here is driver-owned: duplicate-instance guard,
first-completed ordering regardless of submission order, per-instance
failure isolation, and advisory claim release that never masks results.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from redteam.core.enums import AgentPhase
from redteam.core.ids import (
    AgentInstanceId,
    EngagementId,
    MissionId,
    RunId,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.instance_driver import InstanceDriver, InstanceEvent
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason


def _runtime(
    instance: str | None,
    *,
    run_id: str = "run-n",
    engagement_id: str = "eng-n",
    mission_id: str | None = "mission-n",
) -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        mission_id=MissionId(mission_id) if mission_id is not None else None,
        instance_id=AgentInstanceId(instance) if instance is not None else None,
    )


def _driver() -> InstanceDriver:
    # Real deps: runner construction below is stubbed per test, but the
    # driver itself is always built honestly.
    from tests.integration.test_agent_loop import _engagement, _loop_deps

    return InstanceDriver(
        _loop_deps(_engagement()),
        settings=OrchestrationSettings(checkpointing=False),
    )


class _StubDriver(InstanceDriver):
    """Driver with keyed stub runners inside the real owned lifecycle."""

    def __init__(
        self,
        deps: RuntimeDeps,
        *,
        settings: OrchestrationSettings | None = None,
        checkpointer: Any | None = None,
        runners: dict[str, AgentLoopRunner],
    ) -> None:
        super().__init__(deps, settings=settings, checkpointer=checkpointer)
        self._stub_runners = runners

    def runner_for(self, runtime: RuntimeState) -> AgentLoopRunner:
        if not self._opened:
            raise RuntimeError("InstanceDriver is not open")
        key = str(runtime.instance_id)
        if key not in self._runners:
            self._runners[key] = self._stub_runners[key]
        return self._runners[key]


def _stub_driver(runners: dict[str, AgentLoopRunner]) -> InstanceDriver:
    from tests.integration.test_agent_loop import _engagement, _loop_deps

    return _StubDriver(
        _loop_deps(_engagement()),
        settings=OrchestrationSettings(checkpointing=False),
        runners=runners,
    )


class _FakeRunner(AgentLoopRunner):
    """Structural double: only the overridden entries below ever run."""

    def __init__(self, action: Any) -> None:
        self._action = action

    async def run(
        self, runtime: RuntimeState, *, initial_event_id: Any = None
    ) -> RuntimeState:
        return await self._action(runtime)


class _ResumeFakeRunner(_FakeRunner):
    """Records whether the safe or the reconciled resume entry was used."""

    def __init__(self, action) -> None:
        super().__init__(action)
        self.entries: list[str] = []

    async def resume_recovered(self, runtime: RuntimeState) -> RuntimeState:
        self.entries.append("recovered")
        return await self._action(runtime)

    async def resume_reconciled(self, runtime: RuntimeState) -> RuntimeState:
        self.entries.append("reconciled")
        return await self._action(runtime)


async def test_duplicate_instance_rejected() -> None:
    driver = _driver()
    await driver.open()
    try:
        with pytest.raises(ValueError, match="duplicate drive instance"):
            async for _ in driver.drive([_runtime("a"), _runtime("a")]):
                pass
    finally:
        await driver.close()


async def test_results_surface_as_ready_in_completion_order() -> None:
    release = asyncio.Event()

    async def slow(runtime: RuntimeState) -> RuntimeState:
        await release.wait()
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    async def fast(runtime: RuntimeState) -> RuntimeState:
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    driver = _stub_driver({"slow": _FakeRunner(slow), "fast": _FakeRunner(fast)})
    await driver.open()
    try:
        events = []

        async def collect() -> None:
            async for event in driver.drive([_runtime("slow"), _runtime("fast")]):
                events.append(event)
                if event.instance_id == "fast":
                    release.set()

        await asyncio.wait_for(collect(), timeout=10)
    finally:
        await driver.close()
    # The slow instance was submitted first but the fast result arrives first:
    # one instance's long job never gates another's result.
    assert [event.instance_id for event in events] == ["fast", "slow"]
    assert all(event.kind == "completed" for event in events)


async def test_per_instance_failure_isolated() -> None:
    async def boom(runtime: RuntimeState) -> RuntimeState:
        raise RuntimeError("tool exploded")

    async def ok(runtime: RuntimeState) -> RuntimeState:
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    driver = _stub_driver({"bad": _FakeRunner(boom), "good": _FakeRunner(ok)})
    await driver.open()
    try:
        events = [
            event async for event in driver.drive([_runtime("bad"), _runtime("good")])
        ]
    finally:
        await driver.close()
    by_id = {str(event.instance_id): event for event in events}
    assert by_id["bad"].kind == "failed"
    # Repair 1: only the exception TYPE surfaces, never raw exception text.
    assert by_id["bad"].error == "RuntimeError"
    assert by_id["good"].kind == "completed"
    assert by_id["good"].runtime.phase is AgentPhase.FINISHED
    # Repair 1: each event carries its OWN instance runtime.
    assert by_id["bad"].runtime.instance_id == "bad"
    assert by_id["good"].runtime.instance_id == "good"


async def test_cohort_validation_rejects_ownership_violations() -> None:
    driver = _driver()
    await driver.open()
    try:

        async def collect(runtimes):
            return [event async for event in driver.drive(runtimes)]

        # Repair 3: instance_id=None can hold no lease and owns no thread.
        with pytest.raises(ValueError, match="instance_id"):
            await collect([_runtime(None)])
        # Repair 3: one cohort, one run/mission/engagement.
        with pytest.raises(ValueError, match="one run/mission/engagement"):
            await collect([_runtime("a"), _runtime("b", run_id="run-other")])
        with pytest.raises(ValueError, match="one run/mission/engagement"):
            await collect([_runtime("a"), _runtime("b", mission_id="mission-other")])
        with pytest.raises(ValueError, match="one run/mission/engagement"):
            await collect([_runtime("a"), _runtime("b", engagement_id="eng-other")])
        with pytest.raises(ValueError, match="at least one runtime"):
            await collect([])
    finally:
        await driver.close()


async def test_runner_registry_reuse_and_stop_instance() -> None:
    # Repair 4: stop_instance must reach the actually-driving runner, so
    # runner_for reuses one owned runner per instance id.
    rec = _RecordingRunner()
    driver = _stub_driver({"a": rec})
    await driver.open()
    try:
        runtime = _runtime("a", run_id="run-reg", engagement_id="eng_loop_llm")
        first = driver.runner_for(runtime)
        assert driver.runner_for(runtime) is first
        assert driver.stop_instance("missing") is False
        # stop_instance reaches OUR owned runner, not a fresh build.
        assert driver.stop_instance("a") is False
        assert rec.stop_calls == ["stop"]
    finally:
        await driver.close()


class _RecordingRunner(AgentLoopRunner):
    """Owned-runner double that records stop delivery."""

    def __init__(self) -> None:
        self.stop_calls: list[str] = []

    def request_stop(self) -> bool:
        self.stop_calls.append("stop")
        return False


class _StoppableFakeRunner(_FakeRunner):
    """Fake run honoring request_stop like a real runner (graceful finish)."""

    def __init__(self, action) -> None:
        super().__init__(action)
        self._stop = asyncio.Event()

    def request_stop(self) -> bool:
        self._stop.set()
        return True

    async def run(
        self, runtime: RuntimeState, *, initial_event_id: Any = None
    ) -> RuntimeState:
        work = asyncio.create_task(self._action(runtime))
        stop = asyncio.create_task(self._stop.wait())
        done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        if stop in done:
            work.cancel()
            return runtime.model_copy(
                update={
                    "phase": AgentPhase.FINISHED,
                    "termination_reason": TerminationReason.USER_INTERRUPTED,
                }
            )
        stop.cancel()
        return work.result()


async def test_early_abandon_keeps_instances_running_close_waits() -> None:
    # Repair 4: breaking off the iterator must NOT cancel the other
    # instance; close() stops actors first and waits before releasing SDK.
    release = asyncio.Event()

    async def slow(runtime: RuntimeState) -> RuntimeState:
        await release.wait()
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    async def fast(runtime: RuntimeState) -> RuntimeState:
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    driver = _stub_driver(
        {"slow": _StoppableFakeRunner(slow), "fast": _StoppableFakeRunner(fast)}
    )
    await driver.open()
    try:
        iterator = driver.drive([_runtime("slow"), _runtime("fast")])
        first = await iterator.__anext__()
        assert first.instance_id == "fast"
        # Abandon the iterator: slow keeps running under driver ownership.
        await iterator.aclose()
        assert len(driver._active) == 1
        slow_task = next(iter(driver._active))
        assert not slow_task.done() and not slow_task.cancelled()
        release.set()
        await driver.close()
        assert not driver._active
        # Duplicate close is safe.
        await driver.close()
    finally:
        release.set()
        await driver.close()


async def test_release_instance_claim_is_explicit_and_scoped() -> None:
    # Repair 2: return is NOT settlement -- release happens only through
    # this explicit call (the TASK-007 reconcile seam), and it is scoped to
    # runtimes that actually carry a mission task.
    driver = _driver()
    await driver.open()
    try:
        # No mission task: no store touched, no error.
        await driver.release_instance_claim(_runtime("a", mission_id=None))
    finally:
        await driver.close()


async def test_second_iterator_rejects_when_first_drives() -> None:
    # Two iterators built while idle: whichever is consumed second must
    # reject WITHOUT invoking any provider for the duplicate instance.
    invoked: list[str] = []
    release = asyncio.Event()

    async def slow(runtime: RuntimeState) -> RuntimeState:
        invoked.append(str(runtime.instance_id))
        await release.wait()
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    # Stoppable double: close() can always own teardown via request_stop.
    driver = _stub_driver({"a": _StoppableFakeRunner(slow)})
    await driver.open()
    try:
        first = driver.drive([_runtime("a")])
        second = driver.drive([_runtime("a")])
        async def _head() -> InstanceEvent:
            return await first.__anext__()

        first_head = asyncio.create_task(_head())
        for _ in range(1000):
            if invoked:
                break
            await asyncio.sleep(0.01)
        with pytest.raises(ValueError, match="already driving"):
            await second.__anext__()
        # The duplicate never started: exactly one provider invocation.
        assert invoked == ["a"]
        # Release BEFORE awaiting the head: the head only completes once
        # the slow job finishes.
        release.set()
        head = await asyncio.wait_for(asyncio.shield(first_head), timeout=30)
        assert head.instance_id == "a"
        remainder = [event async for event in first]
        assert remainder == []
        await second.aclose()
    finally:
        # Bounded teardown: release, cancel the head guard, then close --
        # never leave an unbounded await behind.
        release.set()
        if not first_head.done():
            first_head.cancel()
        await asyncio.gather(first_head, return_exceptions=True)
        await asyncio.wait_for(driver.close(), timeout=30)


async def test_drive_resumed_routes_safe_and_ready_entries() -> None:
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

    async def ok(runtime: RuntimeState) -> RuntimeState:
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    safe_stub = _ResumeFakeRunner(ok)
    ready_stub = _ResumeFakeRunner(ok)
    driver = _stub_driver({"safe": safe_stub, "ready": ready_stub})
    await driver.open()
    safe_rt = _runtime("safe")
    ready_rt = _runtime("ready")
    ready_rec = ExecutionReconciliation(runtime=ready_rt, processed_invocations=["inv-1"])
    try:
        events = [
            event async for event in driver.drive_resumed([(safe_rt, None), (ready_rt, ready_rec)])
        ]
    finally:
        await driver.close()
    assert {event.instance_id for event in events} == {"safe", "ready"}
    assert all(event.kind == "completed" for event in events)
    # Safe takes resume_recovered (original POLICY path); ready takes
    # resume_reconciled (fresh OBSERVE) -- never the reverse.
    assert safe_stub.entries == ["recovered"]
    assert ready_stub.entries == ["reconciled"]
    for event in events:
        assert event.runtime.instance_id == event.instance_id


async def test_drive_resumed_rejects_held_before_any_start() -> None:
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

    started: list[str] = []

    async def ok(runtime: RuntimeState) -> RuntimeState:
        started.append(str(runtime.instance_id))
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    driver = _stub_driver({"held": _ResumeFakeRunner(ok)})
    await driver.open()
    held_rec = ExecutionReconciliation(
        runtime=_runtime("held"), pending_invocations=["inv-x"]
    )
    assert not held_rec.ready
    try:
        with pytest.raises(ValueError, match="held reconciliation"):
            driver.drive_resumed([(_runtime("held"), held_rec)])
        assert started == []
    finally:
        await driver.close()
