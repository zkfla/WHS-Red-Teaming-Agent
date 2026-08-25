"""단기 작업 메모리 — 현재 라운드 안에서만 의미 있는 임시 컨텍스트.

라운드가 끝나면 `clear()` 로 비운다. 여기 담긴 값은 episodic/semantic 처럼
누적되지 않는다 — 다음 라운드로 넘겨야 할 것은 semantic/episodic 에 옮겨 적어야 한다.
"""
from __future__ import annotations

from typing import Any


class WorkingMemory:
    def __init__(self) -> None:
        self._notes: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._notes[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._notes.get(key, default)

    def clear(self) -> None:
        self._notes.clear()
