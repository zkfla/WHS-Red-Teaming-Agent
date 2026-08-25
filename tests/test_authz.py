"""oracles/authz.py (INV-2) 단위 테스트. 가짜 그래프·결과만 쓴다."""
from __future__ import annotations

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.models.knowledge import Entitlement
from finredteam.oracles.authz import check_inv_2


def _candidate(principal_id: str, subject_account: str | None) -> CandidateFinding:
    return CandidateFinding(
        id="c1",
        layer="finance",
        attack_class="F1",
        invariants=["INV-2"],
        principal_id=principal_id,
        method="GET",
        path=f"/check_balance/{subject_account}",
        subject_account=subject_account,
    )


def _result(candidate: CandidateFinding, codes: list[int]) -> ExecutionResult:
    return ExecutionResult(candidate=candidate, status_codes=codes)


def test_inv_2_violation_when_no_entitlement_and_access_succeeds():
    graph = KnowledgeGraph()
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="account:222", source="self_created")
    )
    cand = _candidate("attacker_A", "222")
    outcome = check_inv_2(cand, _result(cand, [200]), graph)
    assert outcome.verdict == Verdict.VIOLATION


def test_inv_2_passes_when_own_entitlement():
    graph = KnowledgeGraph()
    graph.add_entitlement(
        Entitlement(principal="attacker_A", relation="entitled_own", obj="account:111", source="self_created")
    )
    cand = _candidate("attacker_A", "111")
    outcome = check_inv_2(cand, _result(cand, [200]), graph)
    assert outcome.verdict == Verdict.PASS


def test_inv_2_passes_when_access_denied():
    graph = KnowledgeGraph()
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="account:222", source="self_created")
    )
    cand = _candidate("attacker_A", "222")
    outcome = check_inv_2(cand, _result(cand, [401]), graph)
    assert outcome.verdict == Verdict.PASS


def test_inv_2_unknown_when_no_one_has_entitlement():
    graph = KnowledgeGraph()  # 아무도 entitled_* 없음 — 공동/법인 계좌일 수 있음
    cand = _candidate("attacker_A", "999")
    outcome = check_inv_2(cand, _result(cand, [200]), graph)
    assert outcome.verdict == Verdict.UNKNOWN


def test_inv_2_unknown_without_subject_account():
    graph = KnowledgeGraph()
    cand = _candidate("attacker_A", None)
    outcome = check_inv_2(cand, _result(cand, [200]), graph)
    assert outcome.verdict == Verdict.UNKNOWN


def test_inv_2_observed_access_does_not_grant_entitlement():
    # ★ §5 핵심 불변식: attacker_A 가 이 계좌를 여러 번 "관측"했다고 해도(observed_read)
    # entitled_read 가 생기지 않으므로 계속 VIOLATION 이어야 한다.
    graph = KnowledgeGraph()
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="account:222", source="self_created")
    )
    graph.add_observed("attacker_A", "account:222", "read")
    graph.add_observed("attacker_A", "account:222", "read")

    cand = _candidate("attacker_A", "222")
    outcome = check_inv_2(cand, _result(cand, [200]), graph)
    assert outcome.verdict == Verdict.VIOLATION
