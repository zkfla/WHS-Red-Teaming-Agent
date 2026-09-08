"""G0 opt-in measurement boundary; never receives or serializes raw results.

This is an offline harness wrapper, not an instrumented production runtime.
Durable/audit errors must never be routed through this best-effort observer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter_ns


class Stage(StrEnum):
    COMMIT = "commit"
    RECOMPUTE = "recompute"
    FRONTIER = "frontier"
    CONTEXT = "context"
    CHECKPOINT = "checkpoint"
    RESOLVE = "resolve"
    DECISION = "decision"


@dataclass(frozen=True)
class Measurement:
    stage: Stage
    elapsed_ns: int | None
    outcome: str
    input_items: int
    input_bytes: int | None


@dataclass
class Collector:
    capacity: int = 1000
    events: list[Measurement] = field(default_factory=list, init=False)
    dropped: int = field(default=0, init=False)

    def __post_init__(self):
        if not 1 <= self.capacity <= 100_000:
            raise ValueError("collector capacity out of bounds")

    def __call__(self, event: Measurement) -> None:
        if len(self.events) >= self.capacity:
            self.dropped += 1
        else:
            self.events.append(event)


def measured[T](
    stage: Stage,
    operation: Callable[[], T],
    *,
    observer: Callable[[Measurement], None] | None = None,
    input_items: int = 0,
    input_bytes: int | None = None,
    clock: Callable[[], int] = perf_counter_ns,
) -> T:
    if not isinstance(stage, Stage):
        raise ValueError("stage must be an allowlisted identifier")
    if input_items < 0 or (input_bytes is not None and input_bytes < 0):
        raise ValueError("input sizes must be nonnegative")
    if observer is None:
        return operation()

    def timestamp() -> int | None:
        try:
            return clock()
        except Exception:
            return None

    start = timestamp()
    outcome = "ERROR"
    try:
        result = operation()
        outcome = "OK"
        return result
    finally:
        end = timestamp()
        elapsed = None if start is None or end is None or end < start else end - start
        event = Measurement(stage, elapsed, outcome, input_items, input_bytes)
        try:
            observer(event)
        except Exception:
            pass  # Non-authoritative diagnostics must not replace the operation's outcome.
