from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class RuntimeClock(Protocol):
    def now(self) -> datetime: ...


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime clock must return timezone-aware datetimes")
    return value


@dataclass(frozen=True)
class FixedClock:
    value: datetime

    def now(self) -> datetime:
        return _require_aware(self.value)


class SystemClock:
    """Explicit wall-clock dependency for production composition.

    Semantic code receives this object rather than calling datetime.now() implicitly.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)
