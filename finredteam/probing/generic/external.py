"""외부 도구(Nuclei, sqlmap 등) 연동 인터페이스 — 실제 연동은 하지 않는다 (§19).

2차 확장 지점. 지금은 인터페이스만 정의해서, 나중에 붙일 때 나머지 코드가
바뀌지 않도록 자리만 잡아 둔다.
"""
from __future__ import annotations

from typing import Protocol


class ExternalScanner(Protocol):
    name: str

    def scan(self, target_base_url: str) -> list[dict]:
        """스캐너별 결과를 정규화된 dict 목록으로 반환한다. 구현체는 여기 없다."""
        ...
