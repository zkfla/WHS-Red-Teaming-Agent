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
    def llm_unavailable(self) -> bool: ...
    def saturated(self) -> bool: ...
    def converged(self) -> bool: ...
    def policy_stop_reason(self) -> str | None: ...


def should_stop(ctx: TerminationContextLike) -> tuple[bool, str]:
    # Engagement Stop Condition 이 제일 먼저다 — LLM이 계속하자고 해도, 예산이
    # 남았어도 우선 멈춘다(engagement §12: "최종 실행 통제권은 LLM이 아니라 코드에").
    policy_reason = ctx.policy_stop_reason()
    if policy_reason:
        return True, policy_reason
    if ctx.budget.rounds_exhausted(ctx.round):
        return True, "max_rounds"
    if ctx.budget.tokens_exhausted(ctx.llm_usage):
        return True, "budget"
    if ctx.target_unhealthy():
        return True, "target_unhealthy"
    # saturated() 보다 먼저 본다 — LLM 호출이 계속 실패해서 그래프가 안 자란 것과
    # (예: 레이트리밋) LLM이 정상적으로 응답하는데 탐색이 정체된 것은 원인이
    # 다르다. 뭉뚱그려 "saturated" 로만 남으면 나중에 로그 없이는 구분이 안 된다.
    if ctx.llm_unavailable():
        return True, "llm_unavailable"
    if ctx.saturated():
        return True, "saturated"
    if ctx.converged():
        return True, "converged"
    return False, ""
