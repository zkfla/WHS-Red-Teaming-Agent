"""전역 설정 + 안전장치.

§16의 안전장치 상수는 의도적으로 하드코딩한다 — 설정 파일이나 CLI로 바꿀 수 없다.
LLM 설정만 target yaml의 settings.llm 및 CLI 인자로 교체 가능하다 (§2).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

# ── 안전장치 (§16) — 절대 설정으로 빼지 않는다 ──────────────────────
ALLOWED_HOSTS: set[str] = {"localhost", "127.0.0.1"}
MAX_AMOUNT = Decimal("1000")
MAX_PROBES = 10
MAX_CONCURRENT = 10
RACE_TRIALS = 5
RACE_MIN_INTERVAL = 1.0
REQUEST_TIMEOUT = 10.0  # 대상 애플리케이션(target) 호출용
LLM_REQUEST_TIMEOUT = 60.0  # LLM API 호출용 — 추론엔 대상 요청보다 오래 걸릴 수 있다
MAX_ROUNDS = 12
MAX_TOKENS_TOTAL = 500_000


class AllowedHostError(Exception):
    """요청 대상 호스트가 allowlist 밖일 때 즉시 던진다."""


def assert_allowed_host(url: str) -> None:
    host = urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise AllowedHostError(f"host not allowed: {host!r} (url={url!r})")


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.3
    max_tokens: int = 4096


class RunConfig(BaseModel):
    """단일 실행(run)의 설정. targets/loader.py 의 TargetConfig 와 CLI 인자를 합친 결과."""

    target_name: str
    target_config_path: Path
    llm: LLMConfig = LLMConfig()
    smoke: bool = False
    max_rounds: int = MAX_ROUNDS
    max_tokens_total: int = MAX_TOKENS_TOTAL
