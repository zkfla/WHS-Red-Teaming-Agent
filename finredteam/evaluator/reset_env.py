"""환경 초기화 — 에이전트와 완전히 분리된 evaluator 전용 유틸 (§9, §13).

에이전트 코드(`finredteam/agent/`, `finredteam/probing/` 등)는 이 모듈을 import
하지 않는다 — 그러면 Black-box 원칙이 깨진다. 시나리오 사이 상태 복원(§7 F4 규칙 2)
도 여기서 한다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def reset_target(compose_dir: Path, services: tuple[str, ...] = ("web", "db")) -> None:
    """대상 컨테이너를 재기동해 시드 상태로 되돌린다 (`docker compose restart`)."""
    subprocess.run(
        ["docker", "compose", "restart", *services],
        cwd=str(compose_dir),
        check=True,
        timeout=120,
    )
