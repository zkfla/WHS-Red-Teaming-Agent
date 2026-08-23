"""Anthropic API 얇은 래퍼. 여기서만 API를 부르고, 부를 때마다 계측한다.

모델마다 지원 파라미터가 다르다(아래 CAPS). 이걸 무시하고 통일해서 보내면
haiku 쪽에서 400이 난다 — 티어링을 쓰는 이상 반드시 필요한 분기.
"""
import os
import time

import anthropic
from anthropic import APIStatusError, APIConnectionError, RateLimitError

import config

# 모델별 지원 파라미터. effort/adaptive thinking은 최신 세대에만 있다.
CAPS = {
    "claude-opus-5":    {"effort": True, "adaptive_thinking": True},
    "claude-sonnet-5":  {"effort": True, "adaptive_thinking": True},
    "claude-haiku-4-5": {"effort": False, "adaptive_thinking": False},
}

_client = None


def client() -> anthropic.Anthropic:
    """자격증명 두 종류를 자동 판별한다.

    sk-ant-api…  = API 키       → x-api-key 헤더 (SDK 기본)
    sk-ant-oat…  = OAuth 토큰   → Authorization: Bearer + oauth 베타 헤더
    잘못 보내면 401 'API key is invalid'가 나는데, 원인이 키 값이 아니라
    인증 방식이라 메시지만 보고는 알아채기 어렵다.
    """
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY가 설정되지 않았습니다. .env를 만들거나 export 하세요."
            )
        common = {"max_retries": 4, "timeout": 180.0}
        if key.startswith("sk-ant-oat"):
            _client = anthropic.Anthropic(
                api_key=None, auth_token=key,
                default_headers={"anthropic-beta": "oauth-2025-04-20"},
                **common)
        else:
            _client = anthropic.Anthropic(api_key=key, **common)
    return _client


def call(*, role_cfg, system: str, messages: list, tools: list = None,
         run_ctx=None, max_tokens: int = 16000):
    """한 번의 API 호출. 응답 객체를 그대로 돌려주고, usage는 run_ctx에 적립한다."""
    model = role_cfg.model
    caps = CAPS.get(model, {"effort": False, "adaptive_thinking": False})

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        # system은 실행 내내 고정 → 캐시 브레이크포인트를 여기 둔다.
        # (렌더 순서 tools → system → messages 이므로 tools도 함께 캐시된다)
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
        # ReAct 루프의 진짜 절약 지점: 매 턴 대화가 통째로 길어지므로
        # 마지막 블록에 자동 브레이크포인트를 걸어 이전 턴 전체를 캐시에서 읽는다.
        # (system 949토큰만으로는 최소 캐시 기준에 못 미쳐 적중하지 않는다)
        "cache_control": {"type": "ephemeral"},
    }
    if tools:
        kwargs["tools"] = tools
    if caps["effort"]:
        kwargs["output_config"] = {"effort": role_cfg.effort}
    if caps["adaptive_thinking"]:
        kwargs["thinking"] = {"type": "adaptive"}

    last_err = None
    for attempt in range(6):
        try:
            resp = client().messages.create(**kwargs)
            if run_ctx:
                run_ctx.record_llm(model, resp.usage)
            return resp
        except RateLimitError as e:
            # 서버가 알려주는 대기 시간을 우선 존중한다. 없으면 지수 백오프(최대 60s).
            wait = _retry_after(e) or min(60, 5 * 2 ** attempt)
            last_err = e
            if run_ctx:
                run_ctx.event("rate_limit", model=model, attempt=attempt + 1, wait=wait)
            time.sleep(wait)
        except APIConnectionError as e:
            last_err = e
            time.sleep(min(30, 3 * 2 ** attempt))
        except APIStatusError as e:
            # 400류는 재시도해도 같으므로 즉시 올린다 (파라미터 실수를 숨기지 않기 위해)
            if e.status_code < 500:
                raise
            last_err = e
            time.sleep(min(30, 3 * 2 ** attempt))
    raise last_err


def _retry_after(err) -> float | None:
    try:
        v = err.response.headers.get("retry-after")
        return min(120.0, float(v)) if v else None
    except Exception:
        return None


def text_of(resp) -> str:
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()
