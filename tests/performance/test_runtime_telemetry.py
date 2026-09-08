import asyncio

import pytest

from redteam_core.diagnostics import performance as diagnostics
from redteam_core.diagnostics.performance import (
    PerformanceStage,
    observe_performance,
    performance_span,
)
from tests.performance.workloads import Case, prepare


@pytest.mark.parametrize(
    "stage", ["commit", "freshness", "frontier", "context", "checkpoint", "resolve"]
)
def test_actual_operation_is_identical_with_failing_or_enabled_observer(stage):
    operation, verify = prepare(Case(stage, 10))
    expected = verify(operation())
    events = []
    operation, verify = prepare(Case(stage, 10))
    with observe_performance(events.append):
        assert verify(operation()) == expected
    assert len(events) == (10 if stage == "resolve" else 1)
    assert events[0].outcome == "OK"
    assert events[0].elapsed_ns >= 0

    def broken(event):
        raise RuntimeError("diagnostic sink unavailable")

    operation, verify = prepare(Case(stage, 10))
    with observe_performance(broken):
        assert verify(operation()) == expected


def test_clock_failure_and_sensitive_error_do_not_change_operation(monkeypatch):
    def broken_clock():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(diagnostics, "perf_counter_ns", broken_clock)
    error = RuntimeError("secret payload must not leak")

    @performance_span(PerformanceStage.CHECKPOINT)
    def operation():
        raise error

    events = []
    with observe_performance(events.append), pytest.raises(RuntimeError) as caught:
        operation()
    assert caught.value is error
    assert events[0].elapsed_ns is None
    assert events[0].outcome == "ERROR"
    assert "secret" not in repr(events)


async def test_async_observer_isolation_cancellation_and_reset():
    @performance_span(PerformanceStage.RESOLVE)
    async def operation(cancel=False):
        await asyncio.sleep(0)
        if cancel:
            raise asyncio.CancelledError
        return "raw result"

    async def task(cancel):
        events = []
        with observe_performance(events.append):
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await operation(True)
            else:
                assert await operation() == "raw result"
        await operation()
        return events

    first, second = await asyncio.gather(task(False), task(True))
    assert len(first) == len(second) == 1
    assert first[0].outcome == "OK"
    assert second[0].outcome == "ERROR"
    assert "raw result" not in repr(first)


def test_disabled_observer_never_reads_sizes_or_clock(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled instrumentation must not execute")

    monkeypatch.setattr(diagnostics, "perf_counter_ns", forbidden)

    @performance_span(PerformanceStage.DECISION, input_items=forbidden)
    def operation():
        return 7

    assert operation() == 7
