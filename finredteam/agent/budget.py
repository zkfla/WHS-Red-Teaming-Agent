"""토큰·라운드 예산. §16 상수(MAX_ROUNDS, MAX_TOKENS_TOTAL)를 기본값으로 쓴다."""
from __future__ import annotations

from dataclasses import dataclass

from finredteam.config import MAX_ROUNDS, MAX_TOKENS_TOTAL
from finredteam.llm.base import Usage


@dataclass
class Budget:
    max_rounds: int = MAX_ROUNDS
    max_tokens_total: int = MAX_TOKENS_TOTAL

    def tokens_used(self, usage: Usage) -> int:
        return usage.tokens_in + usage.tokens_out

    def tokens_exhausted(self, usage: Usage) -> bool:
        return self.tokens_used(usage) >= self.max_tokens_total

    def rounds_exhausted(self, round_no: int) -> bool:
        return round_no >= self.max_rounds
