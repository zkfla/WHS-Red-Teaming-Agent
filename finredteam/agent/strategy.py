"""실패→전략 전환 (§14) — ReAct 에서는 강제 배치가 아니라 힌트로 작동한다.

과거엔 `choose()`가 라운드마다 STRATEGIES 중 하나를 강제로 골라 후보를
그 틀 안에서만 나열했다. ReAct 로 바뀌면서 "다음에 뭘 할지"는 LLM 의
Thought 가 정한다 — 이 모듈은 같은 신호(그래프 성장, 최근 성공/실패, 미탐색
레이어)를 관찰해서 Observation 에 힌트 한 줄만 끼워 넣는다. 최종 판단은
여전히 LLM 몫이다 — 강제하지 않는다.
"""
from __future__ import annotations

from typing import Protocol


class AgentContextLike(Protocol):
    """suggest_nudge 가 요구하는 최소 인터페이스. agent/react.py::AgentContext 가 구현한다."""

    round: int

    def graph_grew_significantly(self) -> bool: ...
    def recent_violations(self) -> bool: ...
    def successful_operations(self) -> list[str]: ...
    def consecutive_failures(self) -> int: ...
    def least_tried_operations(self) -> list[str]: ...
    def layer_untouched(self, layer: str) -> bool: ...


def suggest_nudge(ctx: AgentContextLike) -> str | None:
    """§14 "실패→전략 전환"의 취지를 ReAct 안에서 유지한다 — 배치 강제 대신 제안으로."""
    if ctx.round <= 1:
        return None
    if ctx.graph_grew_significantly():
        return "힌트: 그래프가 방금 크게 자랐다 — 새로 알게 된 계좌·객체를 노려보라."
    if ctx.recent_violations():
        ops = ", ".join(ctx.successful_operations())
        return f"힌트: 최근 성공한 조합({ops})을 다른 주체/대상으로 더 시도해볼 만하다."
    if ctx.consecutive_failures() >= 2:
        ops = ", ".join(ctx.least_tried_operations())
        return f"힌트: 같은 접근이 2회 연속 성과가 없었다 — 아직 안 써본 operation({ops})으로 전환을 고려하라."
    if ctx.layer_untouched("generic"):
        return "힌트: 아직 Generic Layer(SQLi 등)를 하나도 시도하지 않았다."
    return None
