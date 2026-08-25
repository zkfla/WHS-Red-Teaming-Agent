"""설정 → LLMClient. 벤더별 가격표는 여기에만 둔다 (§2)."""
from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

from finredteam.config import LLMConfig
from finredteam.llm.base import LLMClient, Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# (provider, model) -> (입력 $/1M tokens, 출력 $/1M tokens). 모르는 모델은 0으로 취급.
PRICING: dict[tuple[str, str], tuple[float, float]] = {
    ("anthropic", "claude-sonnet-4-6"): (3.0, 15.0),
    ("anthropic", "claude-opus-4-1"): (15.0, 75.0),
    ("anthropic", "claude-haiku-4-5"): (0.8, 4.0),
    ("openai", "gpt-5"): (5.0, 15.0),
    ("openai", "gpt-5-mini"): (0.25, 2.0),
}


class _PricedLLMClient:
    """벤더 클라이언트를 감싸 usage.cost_estimate_usd 를 PRICING 기준으로 계산한다."""

    def __init__(self, inner: LLMClient, provider: str, model: str) -> None:
        self._inner = inner
        self.provider = provider
        self.model = model

    def complete_json(
        self, system: str, user: str, schema: type[SchemaT], temperature: float = 0.3
    ) -> list[SchemaT]:
        return self._inner.complete_json(system, user, schema, temperature)

    @property
    def usage(self) -> Usage:
        raw = self._inner.usage
        price_in, price_out = PRICING.get((self.provider, self.model), (0.0, 0.0))
        cost = (raw.tokens_in / 1_000_000) * price_in + (raw.tokens_out / 1_000_000) * price_out
        return Usage(
            tokens_in=raw.tokens_in,
            tokens_out=raw.tokens_out,
            requests=raw.requests,
            cost_estimate_usd=cost,
        )


def build_llm_client(cfg: LLMConfig) -> LLMClient:
    """벤더 SDK import 는 이 함수 내부(및 llm/anthropic_.py, llm/openai_.py)로 국한한다."""
    if cfg.provider == "anthropic":
        from finredteam.llm.anthropic_ import AnthropicClient

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않다")
        inner = AnthropicClient(model=cfg.model, api_key=api_key, max_tokens=cfg.max_tokens)
        return _PricedLLMClient(inner, provider="anthropic", model=cfg.model)

    if cfg.provider == "openai":
        from finredteam.llm.openai_ import OpenAIClient

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 환경변수가 설정되어 있지 않다")
        inner = OpenAIClient(model=cfg.model, api_key=api_key, max_tokens=cfg.max_tokens)
        return _PricedLLMClient(inner, provider="openai", model=cfg.model)

    raise ValueError(f"알 수 없는 llm.provider: {cfg.provider!r}")
