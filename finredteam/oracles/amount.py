"""INV-6 — 금액 정합성: 정밀도(통화 최소단위 초과 금지) + 부호(기대 방향과의 일치).

방향 일치 자체의 세밀한 판정은 INV-1B 몫이다. INV-6 은 그보다 가벼운, 통화
정밀도와 부호라는 두 가지 구조적 성질만 독립적으로 검사한다 — expected_delta
가 아직 없어도(=baseline 미확보) 정밀도 검사는 가능하다는 점이 INV-1B 와 다르다.
"""
from __future__ import annotations

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.base import OracleOutcome, passed, unknown, violation


def check_inv_6(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    delta = result.delta
    if delta is None or delta.has_missing():
        return unknown("INV-6", "잔액 관측 실패 (missing 필드 존재)")
    if not delta.balance_delta:
        return unknown("INV-6", "관측된 잔액 변화가 없음")

    precision_offenders = [
        f"{account}: {money.amount} (통화 {money.currency.code} 최소단위 "
        f"{money.currency.minor_unit}자리 초과)"
        for account, money in delta.balance_delta.items()
        if money.quantize().amount != money.amount
    ]
    if precision_offenders:
        return violation("INV-6", "정밀도 위반: " + "; ".join(precision_offenders))

    exp = candidate.expected_delta
    if exp is not None and exp.is_trusted() and candidate.subject_account:
        expected_subject = exp.deltas.get(candidate.subject_account)
        actual_subject = delta.balance_delta.get(candidate.subject_account)
        if expected_subject is not None and actual_subject is not None:
            expected_sign = expected_subject.amount >= 0
            actual_sign = actual_subject.amount >= 0
            if expected_sign != actual_sign:
                return violation(
                    "INV-6",
                    f"부호 불일치: {candidate.subject_account} 기대 {expected_subject} "
                    f"(부호 {'+' if expected_sign else '-'}), 실제 {actual_subject} "
                    f"(부호 {'+' if actual_sign else '-'})",
                )

    return passed("INV-6", "정밀도·부호 이상 없음")
