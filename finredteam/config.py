"""전역 설정 + 안전장치.

§16의 안전장치 상수는 의도적으로 하드코딩한다 — 설정 파일이나 CLI로 바꿀 수 없다.
LLM 설정만 target yaml의 settings.llm 및 CLI 인자로 교체 가능하다 (§2).

★ 허용 호스트(예전의 `ALLOWED_HOSTS`)는 더 이상 여기 전역 상수로 두지 않는다 —
Engagement(`engagement/schema.py::EngagementScope.allowed_hosts`)가 실행마다
명시적으로 선언해야 한다(Black-box 취지를 vuln-bank 밖 실제 대상으로 넓히려면
"기본으로 로컬만 허용"이라는 하드코딩 자체가 말이 안 된다). 대신 숫자/불리언
상한은 여기 그대로 두고, `engagement/compiler.py::compile_policy()` 가
Engagement 값과 이 상한의 교집합(숫자는 min, 위험 계열 불리언은 AND)을 계산해
`CompiledPolicy` 를 만든다 — Engagement 는 이 상한보다 느슨해질 수 없다(§9).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel

# ── 안전장치 (§16) — 절대 설정으로 빼지 않는다. Engagement 는 이 값들의
# 교집합만 취할 수 있다(compile_policy) ──────────────────────────
MAX_AMOUNT = Decimal("1000")
MAX_PROBES = 10
MAX_CONCURRENT = 10
MAX_REQUESTS_PER_SECOND = 20.0
RACE_TRIALS = 5
RACE_MIN_INTERVAL = 1.0
REQUEST_TIMEOUT = 10.0  # 대상 애플리케이션(target) 호출용
LLM_REQUEST_TIMEOUT = 60.0  # LLM API 호출용 — 추론엔 대상 요청보다 오래 걸릴 수 있다
LLM_MAX_RETRIES = 5  # 벤더 SDK 기본값(보통 2)보다 넉넉히 — 레이트리밋 실측 후 상향(§DEBUGGING.md 3.4-a)
MAX_ROUNDS = 12
MAX_TOKENS_TOTAL = 500_000

# 이 세 가지는 지금 코드베이스에 실행 가능한 액션이 아예 없다(레이스/버스트
# 트래픽을 실제로 보내는 ReAct 액션 없음) — Engagement 가 뭐라고 선언하든
# 프로젝트 차원에서 절대 허용하지 않는다. 대응 액션이 생기면 이 상한부터 재검토.
GLOBAL_ALLOW_DOS = False
GLOBAL_ALLOW_RACE_TESTING = False
GLOBAL_ALLOW_BRUTEFORCE = False


class AllowedHostError(Exception):
    """요청 대상 호스트가 allowlist 밖일 때 즉시 던진다."""


def assert_allowed_host(url: str, allowed_hosts: Iterable[str]) -> None:
    """`allowed_hosts` 는 이제 호출부(정책 계층)가 명시적으로 넘긴다 — 이 함수
    자체엔 암묵적 기본값이 없다. 호스트명 정확 비교만 한다(`localhost.evil.com`
    같은 접미사 편법이 안 통하게)."""
    host = urlparse(url).hostname
    if host not in set(allowed_hosts):
        raise AllowedHostError(f"host not allowed: {host!r} (url={url!r})")


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai", "chatgpt_subscription"] = "anthropic"
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
