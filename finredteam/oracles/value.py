"""INV-1A(가치 보존, 통화별) / INV-1B(인가된 가치 흐름, baseline 기준).

★ 자주 틀리는 지점 (§8): INV-1A ≠ INV-1B. 음수 송금(A:+500, V:-500)은 총합이
보존되어 INV-1A 는 통과하지만, 방향이 반대이므로 INV-1B 는 위반된다. F2 의 주
오라클은 INV-1B 다.
"""
from __future__ import annotations

from decimal import Decimal

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.money import Money, tol
from finredteam.oracles.base import OracleOutcome, is_success_status, passed, unknown, violation


def check_inv_1a(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    if candidate.open_flow:
        # 값이 우리가 관측하지 않는 외부 정산 상대(예: 가맹점)로 정상적으로 흘러나가는
        # 열린 흐름 — 한쪽(지불측)만 관측하면 항상 "가치 소멸"처럼 보이므로 보존을
        # 판정할 수 없다. 신호 부족 → UNKNOWN(다른 오라클, 예: INV-4 가 판정한다).
        return unknown("INV-1A", "관측 집합이 폐쇄계가 아님 — 외부 정산 상대를 관측하지 않아 보존 판정 불가")
    delta = result.delta
    if delta is None or delta.has_missing():
        return unknown("INV-1A", "잔액 관측 실패 (missing 필드 존재)")
    if not delta.balance_delta:
        return unknown("INV-1A", "관측된 잔액 변화가 없음")

    # ★ 통화별로 따로 검사한다 (§4 규칙 3, §8 규칙 2) — 통화가 섞인 합계는 의미가 없다.
    totals: dict[str, Money] = {}
    for money in delta.balance_delta.values():
        code = money.currency.code
        if code not in totals:
            totals[code] = Money(amount=Decimal(0), currency=money.currency)
        totals[code] = totals[code] + money

    offenders = [
        (code, total) for code, total in totals.items() if abs(total.amount) > tol(total.currency)
    ]
    if offenders:
        detail = "; ".join(f"{code} 합계 {total}" for code, total in offenders)
        return violation(
            "INV-1A",
            f"통화별 가치 보존 위반 — 관측된 계정들의 델타 합이 0이 아님: {detail}",
            evidence={"totals": {c: str(m.amount) for c, m in totals.items()}},
        )
    return passed("INV-1A", "관측된 모든 통화에서 델타 합이 0 (가치 보존)")


def check_inv_1b(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    exp = candidate.expected_delta
    if exp is None:
        return unknown("INV-1B", "expected_delta 없음 — baseline 학습 전에는 판정 불가")
    if not exp.is_trusted():
        # ★ §6 — LLM 이 정답지를 쓰지 못하게. llm_inferred 는 힌트일 뿐 판정 근거가 아니다.
        return unknown(
            "INV-1B", f"expected_delta.provenance={exp.provenance!r} — baseline_observed 아니면 판정 불가"
        )

    # 같은 논리 거래(logical_txn_id)를 여러 번 성공 실행한 경우: expected_delta 는
    # "1회 인가분"인데, N 번 반복의 인가 총량이 1회분(멱등)인지 N회분인지는 멱등성
    # 문제라 INV-1B 혼자서는 알 수 없다 — INV-4 에 맡기고 여기선 defer 한다.
    if candidate.logical_txn_id and sum(1 for c in result.status_codes if is_success_status(c)) > 1:
        return unknown(
            "INV-1B", "동일 logical_txn_id 반복 실행 — 인가 총량 판정은 멱등성(INV-4)의 영역"
        )

    delta = result.delta
    if delta is None or delta.has_missing():
        return unknown("INV-1B", "잔액 관측 실패 (missing 필드 존재)")

    mismatches: list[str] = []
    for account, expected in exp.deltas.items():
        actual = delta.balance_delta.get(account)
        if actual is None:
            mismatches.append(f"{account}: 관측 없음 (기대 {expected})")
            continue
        if actual.currency.code != expected.currency.code:
            mismatches.append(
                f"{account}: 통화 불일치 (기대 {expected.currency.code}, 실제 {actual.currency.code})"
            )
            continue
        if abs((actual - expected).amount) > tol(expected.currency):
            mismatches.append(f"{account}: 기대 {expected}, 실제 {actual}")

    if mismatches:
        return violation(
            "INV-1B",
            f"baseline 기대 델타(provenance={exp.provenance})와 불일치: " + "; ".join(mismatches),
            evidence={"baseline_ref": exp.baseline_ref or ""},
        )
    return passed("INV-1B", f"baseline 기대 델타(provenance={exp.provenance})와 일치")
