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
    def ready_for_hypothesis_but_none_proposed(self) -> bool: ...
    def baseline_learned_but_never_tried(self) -> list[str]: ...


def suggest_nudge(ctx: AgentContextLike) -> str | None:
    """§14 "실패→전략 전환"의 취지를 ReAct 안에서 유지한다 — 배치 강제 대신 제안으로."""
    # ★ 실측 버그 대응 — BOOTSTRAP 직후 이미 계좌/entitlement 가 있는데도
    # discover_surface/observe_graph 만 반복하다 saturated 로 아무것도 못
    # 실행하고 끝난 사례가 있었다(round 1부터도 성립하므로 아래 round<=1
    # 가드보다 먼저 본다).
    if ctx.ready_for_hypothesis_but_none_proposed():
        return "힌트: 이미 계좌/권한 정보가 있다 — 더 정찰하지 말고 propose_hypothesis 로 넘어가는 걸 우선 고려하라."
    if ctx.round <= 1:
        return None
    if ctx.graph_grew_significantly():
        return "힌트: 그래프가 방금 크게 자랐다 — 새로 알게 된 계좌·객체를 노려보라."
    # ★ 실측 버그 대응 — read_balance 처럼 계속 VIOLATION 이 나오는 operation만
    # 반복하느라, baseline까지 이미 준비된 다른 operation(예: transfer)은 한
    # 번도 안 써보고 끝나는 사례가 있었다. 계속 성과가 나면(아래 recent_
    # violations) "그대로 더 파라" 는 유혹이 커지므로, 이 힌트를 그보다 먼저
    # 본다 — 성공/실패와 무관하게 매 스텝 알려준다(해당 operation을 한 번이라도
    # 시도하면 자동으로 꺼진다).
    untried_baselines = ctx.baseline_learned_but_never_tried()
    if untried_baselines:
        return (
            f"힌트: {', '.join(untried_baselines)} 는 이미 baseline 학습이 끝나 바로 실험 가능한데 "
            "아직 한 번도 propose_hypothesis 로 시도하지 않았다 — 지금 성과가 나는 operation만 "
            "반복하지 말고 이것도 시도해보라."
        )
    if ctx.recent_violations():
        # ★ 다양화 유도 — read_balance 로 BOLA(INV-2)를 확인하고도 같은 대상만
        # 반복하느라, 같은 BOLA 클래스가 있는 다른 엔드포인트(거래내역 조회)를
        # 안 써보고 끝나는 사례가 있었다. 계좌 기반이라 그래프에 이미 있는 계좌로
        # 바로 지목 가능한(=reachable) operation 만 콕 집어 제안한다.
        untried = set(ctx.least_tried_operations())
        reachable_untried = [op for op in ("read_transactions", "api_transactions") if op in untried]
        if reachable_untried:
            return (
                f"힌트: read_balance 로 BOLA(INV-2)를 확인했다면, 같은 계좌를 대상으로 "
                f"{', '.join(reachable_untried)}(거래내역 조회)에도 같은 권한 경계 취약점이 있을 수 있다 "
                "— 같은 대상만 반복하지 말고 이 엔드포인트들로 넓혀보라."
            )
        ops = ", ".join(ctx.successful_operations())
        return f"힌트: 최근 성공한 조합({ops})을 다른 주체/대상으로 더 시도해볼 만하다."
    if ctx.consecutive_failures() >= 2:
        ops = ", ".join(ctx.least_tried_operations())
        return f"힌트: 같은 접근이 2회 연속 성과가 없었다 — 아직 안 써본 operation({ops})으로 전환을 고려하라."
    if ctx.layer_untouched("generic"):
        return "힌트: 아직 Generic Layer(SQLi 등)를 하나도 시도하지 않았다."
    return None
