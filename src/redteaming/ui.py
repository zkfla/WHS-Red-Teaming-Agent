from __future__ import annotations

import os
import sys
import ctypes
from dataclasses import dataclass


class Ansi:
    RED = "\x1b[31m"
    DIM_RED = "\x1b[2;31m"
    CYAN = "\x1b[36m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    DIM = "\x1b[2m"
    RESET = "\x1b[0m"


def _safe(value: str) -> str:
    """Keep the CLI usable on Windows consoles with legacy code pages."""
    encoding = sys.stdout.encoding or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding, errors="replace")


@dataclass
class ConsoleUI:
    plain: bool = False
    phase: str = "idle"

    def __post_init__(self) -> None:
        if os.name == "nt":
            try:
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleOutputCP(65001)
                kernel32.SetConsoleCP(65001)
            except (AttributeError, OSError):
                pass
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        self.color = not self.plain and sys.stdout.isatty() and os.getenv("NO_COLOR") is None

    def paint(self, value: str, color: str) -> str:
        rendered = f"{color}{value}{Ansi.RESET}" if self.color else value
        return _safe(rendered)

    def banner(self, model: str = "not connected", scope: str = "not set") -> None:
        print(self.paint("+--------------------------------------------------------------+", Ansi.RED))
        print(self.paint("| REDTEAMING CONTROL PLANE                                    |", Ansi.RED))
        print(self.paint("| authorized assessment workspace                             |", Ansi.DIM))
        print(self.paint("+--------------------------------------------------------------+", Ansi.RED))
        print(self.paint("pipeline: intake -> recon -> detect -> verify -> evidence -> report", Ansi.DIM))
        self.status(model=model, scope=scope, phase="idle")
        print(self.paint("자연어로 요청하세요 (예: 승인 대상 등록, 정찰 실행, 리포트 생성) | Ctrl+C 일시중지 | 종료", Ansi.DIM))
        print()

    def status(self, *, model: str, scope: str, phase: str | None = None) -> None:
        if phase is not None:
            self.phase = phase
        marker = self.paint("*", Ansi.GREEN if self.phase == "idle" else Ansi.YELLOW)
        print(
            f"{marker} {self.paint('MODEL', Ansi.DIM)} {_safe(model)}  |  "
            f"{self.paint('SCOPE', Ansi.DIM)} {_safe(scope)}  |  "
            f"{self.paint('PHASE', Ansi.DIM)} {_safe(self.phase)}"
        )

    def prompt(self) -> str:
        return input(self.paint("redteam > ", Ansi.RED))

    def assistant(self, message: str) -> None:
        print(self.paint("agent", Ansi.CYAN))
        print(_safe(message))

    def info(self, message: str) -> None:
        print(self.paint(f"- {_safe(message)}", Ansi.DIM))
