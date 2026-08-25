"""targets/configs/*.yaml 에 쓰는 아주 작은 JSONPath 서브셋만 지원한다.

지원: "$.a.b" (단일 값), "$.a[*].b" (배열 각 원소의 필드, 리스트로 반환).
그 이상(필터식, 재귀하강 등)은 지원하지 않는다 — 어댑터 설정이 그 이상을
요구한다면 설정 스키마를 다시 생각해야 한다는 신호다.
"""
from __future__ import annotations

from typing import Any


def get_path(obj: Any, path: str) -> Any:
    if path == "$":
        return obj
    if not path.startswith("$."):
        raise ValueError(f"지원하지 않는 경로 형식: {path!r} ('$.' 로 시작해야 함)")

    tokens = path[2:].split(".")
    current: list[Any] = [obj]
    has_wildcard = False

    for tok in tokens:
        wildcard = tok.endswith("[*]")
        has_wildcard = has_wildcard or wildcard
        key = tok[:-3] if wildcard else tok

        next_current: list[Any] = []
        for item in current:
            if item is None:
                continue
            value = item.get(key) if key and isinstance(item, dict) else item
            if wildcard:
                if isinstance(value, list):
                    next_current.extend(value)
            else:
                next_current.append(value)
        current = next_current

    if has_wildcard:
        return current
    return current[0] if current else None
