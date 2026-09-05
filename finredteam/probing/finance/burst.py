"""동시 요청 발사 + F4(경쟁조건) 반복 시행 오케스트레이션 (§7).

F4 전용 추가 규칙 4가지를 여기서 강제한다:
1. 확률적이므로 RACE_TRIALS 회 반복한다 (1회 실패를 미탐으로 세지 않는다).
2. 매 시행 전 정상 경로(송금)로 잔액을 회복시킨다 — 회복 불가면 UNKNOWN.
3. "현재 잔액 < 단위금액 × 동시요청수" 사전조건을 강제한다 (아니면 시험이 무의미).
4. MAX_CONCURRENT 상한 + 시행 간 RACE_MIN_INTERVAL 로 DoS 오인을 막는다.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from finredteam.config import MAX_CONCURRENT, RACE_MIN_INTERVAL, RACE_TRIALS
from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.money import Money
from finredteam.oracles.base import OracleOutcome
from finredteam.oracles.concurrency import check_inv_1c
from finredteam.probing.finance.state import compute_delta, observe_many
from finredteam.probing.session import Session, SessionError
from finredteam.targets.loader import TargetConfig


def fire_concurrent(session: Session, candidate: CandidateFinding, principal: Principal, n: int) -> list[int]:
    n = max(1, min(n, MAX_CONCURRENT))

    def _one() -> int:
        try:
            ex = session.authed_request(
                candidate.method, candidate.path, principal, json_body=candidate.params
            )
            return ex.status_code
        except SessionError:
            return 0

    with ThreadPoolExecutor(max_workers=n) as pool:
        return [f.result() for f in [pool.submit(_one) for _ in range(n)]]


def _find_transfer_op(target: TargetConfig) -> dict | None:
    return next((o for o in target.baseline_ops() if o.get("op") == "transfer"), None)


def restore_balance(
    session: Session, target: TargetConfig, restorer: Principal, principal: Principal, amount: Money
) -> bool:
    """정상 경로(송금)로 잔액을 회복시킨다. 대상 어댑터에 transfer 연산이 없거나
    실패하면 False — 그 시행은 UNKNOWN 으로 남는다(§7 규칙 2). 에이전트는
    `env_snapshot_restore` 를 호출할 수 없다(Black-box 원칙, §7)."""
    if amount.amount <= 0 or not principal.account_number:
        return True
    transfer_op = _find_transfer_op(target)
    if transfer_op is None:
        return False
    try:
        ex = session.authed_request(
            transfer_op.get("method", "POST"),
            transfer_op["path"],
            restorer,
            json_body={
                "amount": str(amount.amount),
                "to_account": principal.account_number,
                "description": "race-trial-restore",
            },
        )
    except SessionError:
        return False
    return ex.status_code < 400


@dataclass
class RaceTrial:
    trial_no: int
    skipped_reason: str | None = None
    outcome: OracleOutcome | None = None


def run_race_candidate(
    session: Session,
    target: TargetConfig,
    candidate: CandidateFinding,
    principal: Principal,
    watch_accounts: list[str],
    n_concurrent: int,
    *,
    restorer: Principal | None = None,
    trials: int = RACE_TRIALS,
) -> list[RaceTrial]:
    assert candidate.unit_amount is not None and candidate.subject_account is not None
    unit = candidate.unit_amount
    results: list[RaceTrial] = []

    for trial_no in range(1, trials + 1):
        if trial_no > 1:
            time.sleep(RACE_MIN_INTERVAL)

        before = observe_many(session, target, watch_accounts, principal_id=principal.id)
        subject_balance = before.balances.get(candidate.subject_account)
        if subject_balance is None:
            results.append(RaceTrial(trial_no, skipped_reason="잔액 관측 실패"))
            continue

        needed = unit * n_concurrent
        if subject_balance.amount < needed.amount:
            shortfall = needed - subject_balance + unit
            if restorer is None or not restore_balance(session, target, restorer, principal, shortfall):
                results.append(RaceTrial(trial_no, skipped_reason="잔액 부족(회복 불가) — 시행 스킵"))
                continue
            before = observe_many(session, target, watch_accounts, principal_id=principal.id)
            subject_balance = before.balances.get(candidate.subject_account)
            if subject_balance is None or subject_balance.amount < needed.amount:
                results.append(RaceTrial(trial_no, skipped_reason="잔액 회복 후에도 부족 — 시행 스킵"))
                continue

        status_codes = fire_concurrent(session, candidate, principal, n_concurrent)
        after = observe_many(session, target, watch_accounts, principal_id=principal.id)
        delta = compute_delta(before, after)

        result = ExecutionResult(
            candidate=candidate, status_codes=status_codes, before=before, after=after, delta=delta
        )
        results.append(RaceTrial(trial_no, outcome=check_inv_1c(candidate, result)))

    return results


def run_overdraft_race_candidate(
    session: Session,
    target: TargetConfig,
    candidate: CandidateFinding,
    principal: Principal,
    watch_accounts: list[str],
    n_concurrent: int,
    *,
    restorer: Principal | None = None,
    trials: int = RACE_TRIALS,
) -> list[RaceTrial]:
    """Overdraft/double-spend 플레이버(§7, GT F-12): 잔액을 딱 1건분(unit)만 남기고
    나머지를 정상 송금으로 비운 뒤, 동시 N 건을 발사한다. 잔액 검사가 원자적이라면
    1 건만 성공해야 하지만, 경쟁조건이면 여러 건이 같은 잔액을 읽고 통과해 잔액을
    초과 지출한다 — INV-1C 오라클이 `result.before`(1건분 잔액)와 성공 건수로 판정.

    `run_race_candidate`(lost-update)와 셋업이 정반대다: 저쪽은 모든 요청이 성공할
    만큼 잔액을 채우고, 이쪽은 1건분만 남긴다. 매 시행 전 restorer 로 잔액을 회복한
    뒤 다시 1건분으로 비운다(음수 잔액이 된 이전 시행에서 회복)."""
    assert candidate.unit_amount is not None and candidate.subject_account is not None
    unit = candidate.unit_amount
    results: list[RaceTrial] = []

    for trial_no in range(1, trials + 1):
        if trial_no > 1:
            time.sleep(RACE_MIN_INTERVAL)

        # 이전 시행에서 음수가 됐을 수 있으므로 넉넉히 회복한 뒤 1건분만 남긴다.
        if restorer is not None:
            restore_balance(session, target, restorer, principal, unit * n_concurrent)

        before = observe_many(session, target, watch_accounts, principal_id=principal.id)
        subject_balance = before.balances.get(candidate.subject_account)
        if subject_balance is None:
            results.append(RaceTrial(trial_no, skipped_reason="잔액 관측 실패"))
            continue

        if subject_balance.amount > unit.amount:  # 초과분을 정상 송금으로 비운다
            drain = subject_balance - unit
            try:
                session.authed_request(
                    candidate.method, candidate.path, principal,
                    json_body={**candidate.params, "amount": str(drain.amount)},
                )
            except SessionError:
                results.append(RaceTrial(trial_no, skipped_reason="잔액 조정(drain) 실패 — 시행 스킵"))
                continue
            before = observe_many(session, target, watch_accounts, principal_id=principal.id)
            subject_balance = before.balances.get(candidate.subject_account)

        if subject_balance is None or subject_balance.amount < unit.amount:
            results.append(RaceTrial(trial_no, skipped_reason="1건분 잔액 확보 실패 — 시행 스킵"))
            continue

        status_codes = fire_concurrent(session, candidate, principal, n_concurrent)
        after = observe_many(session, target, watch_accounts, principal_id=principal.id)
        delta = compute_delta(before, after)
        result = ExecutionResult(
            candidate=candidate, status_codes=status_codes, before=before, after=after, delta=delta
        )
        results.append(RaceTrial(trial_no, outcome=check_inv_1c(candidate, result)))

    return results


def summarize_race(results: list[RaceTrial]) -> dict:
    judged = [r for r in results if r.outcome is not None]
    violations = sum(1 for r in judged if r.outcome.verdict.value == "VIOLATION")
    return {
        "trials": len(results),
        "judged": len(judged),
        "skipped": len(results) - len(judged),
        "violations": violations,
        "reproduction_rate": round(violations / len(judged), 2) if judged else 0.0,
        "per_trial": [
            {
                "trial": r.trial_no,
                "verdict": r.outcome.verdict.value if r.outcome else "SKIPPED",
                "reason": r.outcome.reason if r.outcome else r.skipped_reason,
            }
            for r in results
        ],
    }
