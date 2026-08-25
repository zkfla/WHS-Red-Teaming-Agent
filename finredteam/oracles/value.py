"""INV-1A(가치 보존, 통화별) / INV-1B(인가된 가치 흐름, baseline 기준).

★ 자주 틀리는 지점 (§8): INV-1A ≠ INV-1B. 음수 송금(A:+500, V:-500)은 총합이
보존되어 INV-1A 는 통과하지만, 방향이 반대이므로 INV-1B 는 위반된다. F2 의 주
오라클은 INV-1B 다.
"""
from __future__ import annotations

from decimal import Decimal

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.money import Money, tol
from finredteam.oracles.base import OracleOutcome, passed, unknown, violation


def check_inv_1a(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
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
