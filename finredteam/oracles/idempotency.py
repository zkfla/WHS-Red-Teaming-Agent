"""INV-4 — 멱등성. 페이로드가 아니라 논리 거래 식별자(logical_txn_id) 기준.

★ 자주 틀리는 지점 (§8 규칙 3): 같은 커피를 3번 결제하는 건 정상이다(같은 payload,
다른 logical_txn_id). 위반은 "같은 logical_txn_id 로 여러 번 시도했는데 side
effect 가 여러 번 발생"하는 경우다. 식별자 필드를 못 찾으면 UNKNOWN.
"""
from __future__ import annotations

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.money import tol
from finredteam.oracles.base import OracleOutcome, is_success_status, passed, unknown, violation


def check_inv_4(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    if not candidate.logical_txn_id:
        return unknown("INV-4", "논리 거래 식별자 필드를 확인하지 못함 (candidate.logical_txn_id 없음)")

    exp = candidate.expected_delta
    if exp is None or not exp.is_trusted():
        return unknown("INV-4", "단일 처리 기준 expected_delta(baseline) 없음")

    n_success = sum(1 for code in result.status_codes if is_success_status(code))
    if n_success == 0:
        return passed("INV-4", "성공 응답이 없어 멱등성 위반이 발생할 수 없음")

    delta = result.delta
    if delta is None or delta.has_missing():
        return unknown("INV-4", "잔액 관측 실패 (missing 필드 존재)")

    mismatches: list[str] = []
    for account, unit_expected in exp.deltas.items():
        actual = delta.balance_delta.get(account)
        if actual is None:
            mismatches.append(f"{account}: 관측 없음 (기대 1회분 {unit_expected})")
            continue
        if abs((actual - unit_expected).amount) > tol(unit_expected.currency):
            mismatches.append(
                f"{account}: 동일 logical_txn_id={candidate.logical_txn_id!r} 로 "
                f"{n_success}회 성공 응답을 받았으나 실제 델타 {actual} != 1회분 {unit_expected}"
            )

    if mismatches:
        return violation(
            "INV-4",
            "멱등성 위반: " + "; ".join(mismatches),
            evidence={"logical_txn_id": candidate.logical_txn_id, "n_success": n_success},
        )
    return passed(
        "INV-4",
        f"{n_success}회 성공 응답에도 logical_txn_id={candidate.logical_txn_id!r} 의 side effect 는 1회분으로 유지됨",
    )
