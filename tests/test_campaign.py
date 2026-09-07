"""Campaign/Objective 오라클 단위 테스트 — 전부 가짜 Finding 만 쓴다 (서버·LLM 불필요).

개별 불변식 위반(Finding)들을 조합해 '공격자 목표 달성'을 판정하는 상위 계층이
목표별로 achieved(VIOLATION)/foothold(ANOMALY)/unknown 을 올바로 내는지 검사한다.
"""
from __future__ import annotations

from finredteam.models.core import Principal, Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Currency, Money
from finredteam.oracles.campaign import (
    check_obj_authz_bypass,
    check_obj_funds_exfil,
    check_obj_pii_exfil,
    check_obj_priv_esc,
    check_obj_value_forge,
    run_all_objectives,
)

USD = Currency(code="USD", minor_unit=2)

# 공격자 계좌 = ATTACKER, victim 계좌 = VICTIM
PRINCIPALS = [
    Principal(id="attacker_A", username="atk", password="x", account_number="ATTACKER"),
    Principal(id="victim_B", username="vic", password="x", account_number="VICTIM"),
]


def _finding(invariant: str, verdict: Verdict, balances_before=None, balances_after=None) -> Finding:
    cand = CandidateFinding(
        id=f"cand-{invariant}",
        layer="finance",
        operation="transfer",
        principal_id="attacker_A",
        method="POST",
        path="/transfer",
    )
    delta = None
    before = after = None
    if balances_before is not None and balances_after is not None:
        before = ObservedState(
            principal_id="attacker_A",
            balances={k: Money.parse(v, USD) for k, v in balances_before.items()},
        )
        after = ObservedState(
            principal_id="attacker_A",
            balances={k: Money.parse(v, USD) for k, v in balances_after.items()},
        )
        delta = StateDelta.compute(before, after)
    result = ExecutionResult(
        candidate=cand, status_codes=[200], before=before, after=after, delta=delta
    )
    return Finding(candidate=cand, result=result, verdict=verdict, invariant=invariant)


# ── OBJ-FUNDS-EXFIL — 무단 자금 유출 ────────────────────────────────────


def test_funds_exfil_achieved_when_value_moves_to_attacker():
    # INV-1B 위반: victim -500, attacker +500 → 완결된 유출 체인
    f = _finding(
        "INV-1B",
        Verdict.VIOLATION,
        {"ATTACKER": "1000.00", "VICTIM": "1000.00"},
        {"ATTACKER": "1500.00", "VICTIM": "500.00"},
    )
    out = check_obj_funds_exfil([f], PRINCIPALS)
    assert out.verdict == Verdict.VIOLATION
    assert out.chain == ["INV-1B"]
    assert out.evidence["attacker_gain"] == "500.00"


def test_funds_exfil_foothold_when_only_access_no_movement():
    # INV-2 위반(접근 성공)만 있고 자금 이동 없음 → 발판(ANOMALY)
    f = _finding("INV-2", Verdict.VIOLATION)
    out = check_obj_funds_exfil([f], PRINCIPALS)
    assert out.verdict == Verdict.ANOMALY
    assert out.chain == ["INV-2"]


def test_funds_exfil_unknown_without_relevant_violations():
    out = check_obj_funds_exfil([_finding("INV-9", Verdict.VIOLATION)], PRINCIPALS)
    assert out.verdict == Verdict.UNKNOWN


def test_funds_exfil_not_credited_to_attacker_is_not_achieved():
    # INV-1B 위반이지만 값이 제3의 계좌로 감 (공격자 계좌 증가 없음) → 유출 목표 미완결
    f = _finding(
        "INV-1B",
        Verdict.VIOLATION,
        {"ATTACKER": "1000.00", "VICTIM": "1000.00", "OTHER": "0.00"},
        {"ATTACKER": "1000.00", "VICTIM": "500.00", "OTHER": "500.00"},
    )
    out = check_obj_funds_exfil([f], PRINCIPALS)
    assert out.verdict != Verdict.VIOLATION


# ── OBJ-VALUE-FORGE — 가치 창출 ────────────────────────────────────────


def test_value_forge_achieved_when_attacker_gains_without_source():
    # INV-1A 위반: 대응 감소 없이 공격자만 +300 → 가치 생성
    f = _finding(
        "INV-1A",
        Verdict.VIOLATION,
        {"ATTACKER": "1000.00", "VICTIM": "1000.00"},
        {"ATTACKER": "1300.00", "VICTIM": "1000.00"},
    )
    out = check_obj_value_forge([f], PRINCIPALS)
    assert out.verdict == Verdict.VIOLATION
    assert "INV-1A" in out.chain
    assert out.evidence["attacker_gain"] == "300.00"


def test_value_forge_distinct_from_exfil_when_source_debited():
    # 공격자 +500 인데 victim -500 이 있으면 '창출'이 아니라 '유출' → forge 는 부분(ANOMALY)
    f = _finding(
        "INV-1B",
        Verdict.VIOLATION,
        {"ATTACKER": "1000.00", "VICTIM": "1000.00"},
        {"ATTACKER": "1500.00", "VICTIM": "500.00"},
    )
    # INV-1B 는 forge 대상 불변식이 아니므로 UNKNOWN 이어야 한다 (유출은 exfil 이 판정)
    out = check_obj_value_forge([f], PRINCIPALS)
    assert out.verdict == Verdict.UNKNOWN


def test_value_forge_via_idempotency_duplicate_credit():
    f = _finding(
        "INV-4",
        Verdict.VIOLATION,
        {"ATTACKER": "1000.00"},
        {"ATTACKER": "1100.00"},
    )
    out = check_obj_value_forge([f], PRINCIPALS)
    assert out.verdict == Verdict.VIOLATION
    assert "INV-4" in out.chain


# ── 단일 불변식 목표들 ─────────────────────────────────────────────────


def test_authz_bypass_and_priv_esc_and_pii():
    assert check_obj_authz_bypass([_finding("INV-2", Verdict.VIOLATION)], PRINCIPALS).verdict == Verdict.VIOLATION
    assert check_obj_authz_bypass([], PRINCIPALS).verdict == Verdict.UNKNOWN
    assert check_obj_priv_esc([_finding("INV-BFLA", Verdict.VIOLATION)], PRINCIPALS).verdict == Verdict.VIOLATION
    assert check_obj_pii_exfil([_finding("INV-9", Verdict.VIOLATION)], PRINCIPALS).verdict == Verdict.VIOLATION


def test_non_violation_findings_are_ignored():
    # ANOMALY/UNKNOWN Finding 은 목표 판정 재료가 아니다 (VIOLATION 만 체인 구성)
    fs = [_finding("INV-2", Verdict.ANOMALY), _finding("INV-BFLA", Verdict.UNKNOWN)]
    assert check_obj_authz_bypass(fs, PRINCIPALS).verdict == Verdict.UNKNOWN
    assert check_obj_priv_esc(fs, PRINCIPALS).verdict == Verdict.UNKNOWN


# ── run_all_objectives — 전량 실행 ─────────────────────────────────────


def test_run_all_objectives_runs_every_objective_unconditionally():
    out = run_all_objectives([_finding("INV-2", Verdict.VIOLATION)], PRINCIPALS)
    objs = {o.objective for o in out}
    assert objs == {
        "OBJ-FUNDS-EXFIL",
        "OBJ-VALUE-FORGE",
        "OBJ-AUTHZ-BYPASS",
        "OBJ-PRIV-ESC",
        "OBJ-PII-EXFIL",
    }
    by_id = {o.objective: o for o in out}
    # INV-2 하나로: 권한우회는 달성, 자금유출은 발판, 나머지는 재료부족(UNKNOWN)
    assert by_id["OBJ-AUTHZ-BYPASS"].verdict == Verdict.VIOLATION
    assert by_id["OBJ-FUNDS-EXFIL"].verdict == Verdict.ANOMALY
    assert by_id["OBJ-PRIV-ESC"].verdict == Verdict.UNKNOWN
    assert by_id["OBJ-VALUE-FORGE"].verdict == Verdict.UNKNOWN
