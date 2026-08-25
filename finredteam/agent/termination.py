"""자율적 종료 판단 (§14). 고정 횟수만 돌고 끝나면 스캐너와 다를 게 없다 — 예산·포화·
수렴·대상 이상 4가지 독립 신호를 본다. 종료 사유는 반드시 매니페스트에 남긴다.
"""
from __future__ import annotations

from typing import Protocol

from finredteam.agent.budget import Budget
from finredteam.llm.base import Usage


class TerminationContextLike(Protocol):
    round: int
    budget: Budget

    @property
    def llm_usage(self) -> Usage: ...
    def target_unhealthy(self) -> bool: ...
    def saturated(self) -> bool: ...
    def converged(self) -> bool: ...


def should_stop(ctx: TerminationContextLike) -> tuple[bool, str]:
    if ctx.budget.rounds_exhausted(ctx.round):
        return True, "max_rounds"
    if ctx.budget.tokens_exhausted(ctx.llm_usage):
        return True, "budget"
    if ctx.target_unhealthy():
        return True, "target_unhealthy"
    if ctx.saturated():
        return True, "saturated"
    if ctx.converged():
        return True, "converged"
    return False, ""
