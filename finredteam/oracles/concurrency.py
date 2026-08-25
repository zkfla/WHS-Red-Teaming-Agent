"""INV-1C — 동시 실행 정합성. F4 전용 (§7).

성공 응답 N건 × 단위 금액 == 실제 상태 변화 여야 한다. 확률적으로만 재현되므로
호출부(probing/finance/burst.py)가 여러 번 반복 시행해서 재현율로 보고한다 —
이 오라클 자체는 시행 1회를 판정할 뿐이다.
"""
from __future__ import annotations

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.money import tol
from finredteam.oracles.base import OracleOutcome, anomaly, is_success_status, passed, unknown, violation


def check_inv_1c(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    if candidate.unit_amount is None or candidate.subject_account is None:
        return unknown("INV-1C", "candidate.unit_amount 또는 subject_account 없음")

    delta = result.delta
    if delta is None or delta.has_missing():
        return unknown("INV-1C", "잔액 관측 실패 (missing 필드 존재)")

    n_ok = sum(1 for code in result.status_codes if is_success_status(code))
    unit = candidate.unit_amount
    expected = unit * n_ok

    if n_ok == 0:
        return passed("INV-1C", "성공 응답 없음 — 동시성 제어 정상")

    actual_money = delta.balance_delta.get(candidate.subject_account)
    if actual_money is None:
        return unknown("INV-1C", f"{candidate.subject_account} 잔액 변화 관측 없음")
    actual = abs(actual_money)

    tolerance = tol(unit.currency)
    evidence = {
        "n_success": n_ok,
        "unit_amount": str(unit.amount),
        "expected": str(expected.amount),
        "actual": str(actual.amount),
    }

    if actual.amount < expected.amount - tolerance:
        return violation(
            "INV-1C",
            f"성공 {n_ok}건 x {unit} = {expected} 기대, 실제 차감 {actual} "
            f"-> {(expected - actual).amount} 만큼 초과 인출",
            evidence=evidence,
        )
    if actual.amount > expected.amount + tolerance:
        return anomaly(
            "INV-1C",
            f"차감이 성공 건수보다 많음 ({actual} > {expected}) — 이중 차감 의심",
            evidence=evidence,
        )
    return passed("INV-1C", f"성공 {n_ok}건의 차감이 기대치와 정합함")
