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

    # subject_account 는 원래 계좌번호만 가정했지만(예: "1234567890" -> "account:1234567890"),
    # 카드 등 계좌가 아닌 객체의 BOLA 도 같은 오라클로 판정하려면 완전한 그래프 객체 id를
    # 그대로 받을 수 있어야 한다("card:42" 처럼). 이미 콜론이 있으면 그대로 쓰고, 없으면
    # 기존처럼 "account:" 를 붙인다 — 계좌 기반 candidate 는 동작이 그대로다.
    subject = candidate.subject_account
    obj = subject if ":" in subject else f"account:{subject}"
    if graph.has_entitlement(candidate.principal_id, obj, "read"):
        return passed("INV-2", f"{candidate.principal_id} 는 {obj} 에 entitled_read/own 보유")

    if graph.entitlement_unknown(obj):
        return unknown("INV-2", f"{obj} 의 정상 권한을 확정하지 못함 — 공동/법인 계좌 등일 수 있음")

    return violation(
        "INV-2",
        f"{candidate.principal_id} 는 {obj} 에 대한 entitlement 없이 접근에 성공함",
        evidence={"principal": candidate.principal_id, "object": obj},
    )
