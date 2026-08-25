"""로깅 설정 — 콘솔(레벨 조절 가능) + 파일(항상 DEBUG, `runs/<run_id>/agent.log`).

`__main__.py::main()` 에서 한 번만 호출한다. 사람이 디버깅할 때만 쓰는
채널이다 — 판정 파이프라인은 로그를 읽지 않는다(§ DEBUGGING.md §2).
"""
from __future__ import annotations

import logging
from pathlib import Path

_LOGGER_NAME = "finredteam"


def configure_logging(run_dir: Path, console_level: str = "WARNING") -> None:
    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(logging.DEBUG)  # 실제 필터링은 핸들러가 각자 한다
    root.handlers.clear()
    root.propagate = False  # 파이썬 기본 root 로거로 더 안 새게(중복 출력 방지)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, console_level))
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console_handler)

    run_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(run_dir / "agent.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
