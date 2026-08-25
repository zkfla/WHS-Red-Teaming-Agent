"""INV-2 — 권한 경계. `entitled_*` 로 연결되지 않은 객체 접근을 위반으로 본다 (§5).

entitlement 을 확정하지 못한 객체는 UNKNOWN 이다 — 위반으로 처리하지 않는다.
공동계좌·법인계좌처럼 정당한 접근일 수 있어서다. 다른 오라클과 달리 그래프가
필요하다 — `oracles/registry.py` 의 `GRAPH_REGISTRY` 를 통해 별도로 디스패치된다.
"""
from __future__ import annotations

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.base import OracleOutcome, is_success_status, passed, unknown, violation


def check_inv_2(candidate: CandidateFinding, result: ExecutionResult, graph: KnowledgeGraph) -> OracleOutcome:
    if candidate.subject_account is None:
        return unknown("INV-2", "candidate.subject_account 없음 — 접근 대상 객체를 특정할 수 없음")

    if not any(is_success_status(code) for code in result.status_codes):
        return passed("INV-2", "접근이 성공하지 않음 — 권한 경계 정상")

    obj = f"account:{candidate.subject_account}"
    if graph.has_entitlement(candidate.principal_id, obj, "read"):
        return passed("INV-2", f"{candidate.principal_id} 는 {obj} 에 entitled_read/own 보유")

    if graph.entitlement_unknown(obj):
        return unknown("INV-2", f"{obj} 의 정상 권한을 확정하지 못함 — 공동/법인 계좌 등일 수 있음")

    return violation(
        "INV-2",
        f"{candidate.principal_id} 는 {obj} 에 대한 entitlement 없이 접근에 성공함",
        evidence={"principal": candidate.principal_id, "object": obj},
    )
