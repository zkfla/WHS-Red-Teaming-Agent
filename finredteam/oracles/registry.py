"""invariant id → 오라클 함수.

★ `candidate.invariants` 로 미리 좁히지 않는다 — 시스템이 실행 전에 고정해둔
오라클 전체 집합(`REGISTRY` + `GRAPH_REGISTRY`)을 매 실행마다 예외 없이 전부
돌린다. 각 오라클은 자기 판정 재료(예: `logical_txn_id`, `unit_amount`)가
없으면 스스로 UNKNOWN 을 낸다 — "무엇을 검사할지"는 candidate 가 선언하는
게 아니라 **실행 결과 + 그래프가 갖춘 재료**로 각 오라클이 스스로 결정한다.
이게 "오라클은 실행 전에 선언한다"(§설계원칙 3)를 candidate 단위가 아니라
시스템 단위로 더 강하게 지키는 방법이다 — LLM 이 오라클 범위를 스스로 좁혀
불리한 판정을 피해가는 경로 자체가 없다.
"""
from __future__ import annotations

from typing import Callable

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.amount import check_inv_6
from finredteam.oracles.authz import check_inv_2
from finredteam.oracles.base import OracleOutcome
from finredteam.oracles.concurrency import check_inv_1c
from finredteam.oracles.exposure import check_inv_9
from finredteam.oracles.idempotency import check_inv_4
from finredteam.oracles.privilege import check_bfla
from finredteam.oracles.value import check_inv_1a, check_inv_1b

OracleFn = Callable[[CandidateFinding, ExecutionResult], OracleOutcome]
GraphOracleFn = Callable[[CandidateFinding, ExecutionResult, KnowledgeGraph], OracleOutcome]

REGISTRY: dict[str, OracleFn] = {
    "INV-1A": check_inv_1a,
    "INV-1B": check_inv_1b,
    "INV-1C": check_inv_1c,
    "INV-4": check_inv_4,
    "INV-6": check_inv_6,
    "INV-9": check_inv_9,
    "INV-BFLA": check_bfla,
}

GRAPH_REGISTRY: dict[str, GraphOracleFn] = {
    "INV-2": check_inv_2,
}


def run_all(
    candidate: CandidateFinding, result: ExecutionResult, graph: KnowledgeGraph | None = None
) -> list[OracleOutcome]:
    """등록된 오라클을 예외 없이 전부 실행한다 — candidate 는 이 목록을 줄일 수 없다."""
    outcomes = [fn(candidate, result) for fn in REGISTRY.values()]
    if graph is not None:
        outcomes.extend(fn(candidate, result, graph) for fn in GRAPH_REGISTRY.values())
    return outcomes
