"""파일 기반 경량 저장소 (JSONL append + 인덱스). 외부 DB 금지 (§1).

어떤 모듈도 이 파일을 거치지 않고 evidence/결과를 직접 파일에 쓰지 않는다 (§10).
"""
from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

COLLECTIONS = (
    "requests",
    "responses",
    "candidates",
    "executions",
    "states",
    "deltas",
    "verdicts",
    "findings",
    "facts",
    "lessons",
    "entitlements",
    "baselines",
    "steps",  # ReAct 트레이스(thought/action/observation) — ReAct 전환 시 추가
    "llm_calls",  # LLM 호출 원문(prompt/raw response)·이벤트 — 디버깅 전용(DEBUGGING.md)
    "created_resources",  # 실행 중 생성한 리소스(테스트 계정 등) — CLEANUP phase 대상 (engagement §11)
    "cleanup",  # CLEANUP phase 결과 — 성공/실패/미설정 모두 정직하게 기록 (engagement §11)
)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class DB:
    """`run_dir` 아래 컬렉션별 `<name>.jsonl` 파일에 append-only 로 기록한다."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, collection: str) -> Path:
        if collection not in COLLECTIONS:
            raise ValueError(f"알 수 없는 컬렉션: {collection!r} (허용: {COLLECTIONS})")
        return self.run_dir / f"{collection}.jsonl"

    def append(self, collection: str, record: dict[str, Any]) -> int:
        """레코드를 추가하고, 해당 컬렉션 내 0부터 시작하는 순번(offset)을 반환한다."""
        path = self._path(collection)
        with self._lock:
            offset = sum(1 for _ in path.open("r", encoding="utf-8")) if path.exists() else 0
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, cls=_DecimalEncoder, ensure_ascii=False) + "\n")
        return offset

    def read_all(self, collection: str) -> Iterator[dict[str, Any]]:
        path = self._path(collection)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def count(self, collection: str) -> int:
        return sum(1 for _ in self.read_all(collection))
