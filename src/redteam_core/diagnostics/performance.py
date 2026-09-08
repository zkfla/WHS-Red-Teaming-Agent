"""Opt-in numeric spans; never emit prompts, raw payloads or exception messages.

No sink is installed by default. Context variables isolate concurrent callers.
Observer failures are ignored; operation/audit failures are always propagated.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from inspect import iscoroutinefunction
from time import perf_counter_ns


class PerformanceStage(StrEnum):
    COMMIT = "commit"
    RECOMPUTE = "recompute"
    FRONTIER = "frontier"
    CONTEXT = "context"
    CHECKPOINT = "checkpoint"
    RESOLVE = "resolve"
    DECISION = "decision"


@dataclass(frozen=True)
class PerformanceMeasurement:
    stage: PerformanceStage
    elapsed_ns: int | None
    outcome: str
    input_items: int | None
    input_bytes: int | None = None


PerformanceObserver = Callable[[PerformanceMeasurement], None]
_observer: ContextVar[PerformanceObserver | None] = ContextVar("performance_observer", default=None)


@contextmanager
def observe_performance(observer: PerformanceObserver | None) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def _timestamp() -> int | None:
    try:
        return perf_counter_ns()
    except Exception:
        return None


@contextmanager
def _span(observer, stage, input_items, args, kwargs):
    count = None
    if input_items is not None:
        try:
            value = input_items(*args, **kwargs)
            count = value if type(value) is int and value >= 0 else None
        except Exception:
            pass
    start = _timestamp()
    outcome = "ERROR"
    try:
        yield
        outcome = "OK"
    finally:
        end = _timestamp()
        elapsed = None if start is None or end is None or end < start else end - start
        try:
            observer(PerformanceMeasurement(stage, elapsed, outcome, count))
        except Exception:
            pass


def performance_span(stage: PerformanceStage, *, input_items=None):
    if not isinstance(stage, PerformanceStage):
        raise ValueError("performance stage must be allowlisted")

    def decorate(operation):
        if iscoroutinefunction(operation):

            @wraps(operation)
            async def async_wrapper(*args, **kwargs):
                observer = _observer.get()
                if observer is None:
                    return await operation(*args, **kwargs)
                with _span(observer, stage, input_items, args, kwargs):
                    return await operation(*args, **kwargs)

            return async_wrapper

        @wraps(operation)
        def wrapper(*args, **kwargs):
            observer = _observer.get()
            if observer is None:
                return operation(*args, **kwargs)
            with _span(observer, stage, input_items, args, kwargs):
                return operation(*args, **kwargs)

        return wrapper

    return decorate
