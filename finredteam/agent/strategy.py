"""실패→전략 전환 (§14). 막히면 그냥 다음 후보로 넘어가는 대신, 관측된 신호에 따라
탐색 폭/깊이/공격 클래스 구성을 바꾼다 — 이게 없으면 스캐너와 다를 게 없다.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    pass


class AgentContextLike(Protocol):
    """strategy.choose 가 요구하는 최소 인터페이스. agent/loop.py::AgentContext 가 구현한다."""

    round: int

    def has_pending_race_trials(self) -> bool: ...
    def graph_grew_significantly(self) -> bool: ...
    def recent_violations(self) -> bool: ...
    def successful_classes(self) -> list[str]: ...
    def consecutive_failures(self) -> int: ...
    def least_tried_classes(self) -> list[str]: ...
    def layer_untouched(self, layer: str) -> bool: ...


@dataclass(frozen=True)
class Strategy:
    name: str
    attack_classes: list[str]
    batch_size: int
    depth: str


_BROAD_SWEEP = Strategy("BROAD_SWEEP", ["F1", "F2", "F3", "F8", "G-SQLI", "G-BOLA"], 8, "broad")
_DEEPEN_SUCCESS = Strategy("DEEPEN_SUCCESS", [], 5, "deep")
_PIVOT = Strategy("PIVOT", [], 6, "broad")
_EXPLOIT_GRAPH = Strategy("EXPLOIT_GRAPH", ["F1", "F8"], 6, "deep")
_GENERIC_SWEEP = Strategy("GENERIC_SWEEP", ["G-SQLI", "G-BOLA", "G-JWT"], 8, "broad")
_RACE_RETRY = Strategy("RACE_RETRY", ["F4"], 1, "deep")

STRATEGIES: dict[str, Strategy] = {
    s.name: s
    for s in (_BROAD_SWEEP, _DEEPEN_SUCCESS, _PIVOT, _EXPLOIT_GRAPH, _GENERIC_SWEEP, _RACE_RETRY)
}


def choose(ctx: AgentContextLike) -> Strategy:
    if ctx.round == 1:
        return _BROAD_SWEEP
    if ctx.has_pending_race_trials():
        return _RACE_RETRY
    if ctx.graph_grew_significantly():
        return _EXPLOIT_GRAPH
    if ctx.recent_violations():
        return replace(_DEEPEN_SUCCESS, attack_classes=ctx.successful_classes())
    if ctx.consecutive_failures() >= 2:
        return replace(_PIVOT, attack_classes=ctx.least_tried_classes())
    if ctx.layer_untouched("generic"):
        return _GENERIC_SWEEP
    return _BROAD_SWEEP


def suggest_nudge(ctx: AgentContextLike) -> str | None:
    """ReAct 루프용 — "다음에 뭘 할지"를 코드가 강제하는 대신, LLM 의 Observation 에
    끼워 넣을 힌트 한 줄만 만든다. 최종 판단은 여전히 LLM 의 Thought 몫이다.

    `choose()` 와 같은 신호(성공/실패 연속, 그래프 성장, 미탐색 레이어)를 재사용해서
    §14 "실패→전략 전환"의 취지를 ReAct 안에서도 유지한다 — 다만 강제 배치 대신
    제안(nudge)으로만 작동한다.
    """
    if ctx.graph_grew_significantly():
        return "힌트: 그래프가 방금 크게 자랐다 — 새로 알게 된 계좌·객체를 노려보라."
    if ctx.recent_violations():
        classes = ", ".join(ctx.successful_classes())
        return f"힌트: 최근 성공한 공격군({classes})을 다른 주체/대상으로 더 시도해볼 만하다."
    if ctx.consecutive_failures() >= 2:
        classes = ", ".join(ctx.least_tried_classes())
        return f"힌트: 같은 접근이 2회 연속 성과가 없었다 — 다른 공격 클래스({classes})로 전환을 고려하라."
    if ctx.layer_untouched("generic"):
        return "힌트: 아직 Generic Layer(SQLi 등)를 하나도 시도하지 않았다."
    return None
