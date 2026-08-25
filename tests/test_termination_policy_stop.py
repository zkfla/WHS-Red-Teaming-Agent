"""Engagement Stop Condition(repeated_5xx) 이 termination.should_stop() 에서
다른 신호보다 먼저 체크되고, LLM 이 계속하자고 해도(=예산이 남아 있어도) 우선
멈추는지 확인한다(engagement §12)."""
from __future__ import annotations

from dataclasses import dataclass

from finredteam.agent import termination
from finredteam.agent.budget import Budget
from finredteam.llm.base import Usage


@dataclass
class _FakeCtx:
    round: int
    budget: Budget
    _policy_stop: str | None = None
    _unhealthy: bool = False
    _llm_unavailable: bool = False
    _saturated: bool = False
    _converged: bool = False

    @property
    def llm_usage(self) -> Usage:
        return Usage()

    def target_unhealthy(self) -> bool:
        return self._unhealthy

    def llm_unavailable(self) -> bool:
        return self._llm_unavailable

    def saturated(self) -> bool:
        return self._saturated

    def converged(self) -> bool:
        return self._converged

    def policy_stop_reason(self) -> str | None:
        return self._policy_stop


def test_no_stop_when_nothing_triggered():
    ctx = _FakeCtx(round=1, budget=Budget(max_rounds=10))
    stop, reason = termination.should_stop(ctx)
    assert stop is False
    assert reason == ""


def test_policy_stop_triggers_even_with_budget_remaining():
    ctx = _FakeCtx(round=1, budget=Budget(max_rounds=100), _policy_stop="policy_stop:repeated_5xx(>=5)")
    stop, reason = termination.should_stop(ctx)
    assert stop is True
    assert reason == "policy_stop:repeated_5xx(>=5)"


def test_policy_stop_checked_before_max_rounds():
    # 라운드도 소진되고 policy_stop도 걸린 상태 — policy_stop 사유가 나와야 한다(우선순위).
    ctx = _FakeCtx(round=100, budget=Budget(max_rounds=10), _policy_stop="policy_stop:repeated_5xx(>=5)")
    stop, reason = termination.should_stop(ctx)
    assert stop is True
    assert reason == "policy_stop:repeated_5xx(>=5)"


def test_max_rounds_still_works_when_policy_stop_not_triggered():
    ctx = _FakeCtx(round=100, budget=Budget(max_rounds=10))
    stop, reason = termination.should_stop(ctx)
    assert stop is True
    assert reason == "max_rounds"


def test_llm_unavailable_reported_distinctly_from_saturated():
    # 실측 버그: LLM 호출이 계속 실패해도 "saturated" 로만 뭉뚱그려져서 원인이
    # 안 보였다 — llm_unavailable() 이 saturated() 보다 먼저, 별도 사유로 잡혀야 한다.
    ctx = _FakeCtx(round=5, budget=Budget(max_rounds=20), _llm_unavailable=True, _saturated=True)
    stop, reason = termination.should_stop(ctx)
    assert stop is True
    assert reason == "llm_unavailable"
