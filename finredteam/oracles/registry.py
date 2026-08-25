"""invariant id → 오라클 함수. `candidate.invariants` 에 선언된 것만 실행한다
(§설계원칙 3 — 실행 전에 오라클을 선언한다).

대부분의 오라클은 (candidate, result) 만으로 판정하는 순수 함수다. INV-2 만
`KnowledgeGraph` 가 필요해서(§5) 별도의 `GRAPH_REGISTRY` 로 디스패치한다 —
그래프가 없으면(graph=None) 조용히 스킵된다.
"""
from __future__ import annotations

from typing import Callable

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.amount import check_inv_6
from finredteam.oracles.authz import check_inv_2
from finredteam.oracles.base import OracleOutcome
from finredteam.oracles.concurrency import check_inv_1c
from finredteam.oracles.idempotency import check_inv_4
from finredteam.oracles.value import check_inv_1a, check_inv_1b

OracleFn = Callable[[CandidateFinding, ExecutionResult], OracleOutcome]
GraphOracleFn = Callable[[CandidateFinding, ExecutionResult, KnowledgeGraph], OracleOutcome]

# INV-9(exposure) 는 아직 없다 — candidate.invariants 에 있어도 여기 없으면 스킵된다.
REGISTRY: dict[str, OracleFn] = {
    "INV-1A": check_inv_1a,
    "INV-1B": check_inv_1b,
    "INV-1C": check_inv_1c,
    "INV-4": check_inv_4,
    "INV-6": check_inv_6,
}

GRAPH_REGISTRY: dict[str, GraphOracleFn] = {
    "INV-2": check_inv_2,
}


def get(invariant: str) -> OracleFn | None:
    return REGISTRY.get(invariant)


def run_all(
    candidate: CandidateFinding, result: ExecutionResult, graph: KnowledgeGraph | None = None
) -> list[OracleOutcome]:
    outcomes = [REGISTRY[inv](candidate, result) for inv in candidate.invariants if inv in REGISTRY]
    if graph is not None:
        outcomes.extend(
            GRAPH_REGISTRY[inv](candidate, result, graph)
            for inv in candidate.invariants
            if inv in GRAPH_REGISTRY
        )
    return outcomes
