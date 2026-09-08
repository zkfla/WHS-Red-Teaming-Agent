from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def fp(character: str) -> str:
    return "sha256:" + character * 64


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value
