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
