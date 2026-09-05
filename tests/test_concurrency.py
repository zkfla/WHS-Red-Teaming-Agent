"""oracles/concurrency.py (INV-1C) 단위 테스트. 가짜 스냅샷만 쓴다."""
from __future__ import annotations

from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Currency, Money
from finredteam.oracles.concurrency import check_inv_1c

USD = Currency(code="USD", minor_unit=2)


def _state(balances: dict[str, str]) -> ObservedState:
    return ObservedState(
        principal_id="attacker_A", balances={k: Money.parse(v, USD) for k, v in balances.items()}
    )


def _candidate(**overrides) -> CandidateFinding:
    base = dict(
        id="race-1",
        layer="finance",
        operation="F4",
        principal_id="attacker_A",
        method="POST",
        path="/transfer",
        subject_account="ATTACKER",
        unit_amount=Money.parse("100.00", USD),
    )
    base.update(overrides)
    return CandidateFinding(**base)


def _result(candidate: CandidateFinding, before, after, codes: list[int]) -> ExecutionResult:
    return ExecutionResult(
        candidate=candidate, status_codes=codes, before=before, after=after,
        delta=StateDelta.compute(before, after),
    )


def test_inv_1c_violation_on_over_withdrawal():
    # §7 예시: 성공 8건 x 100.00 = 800.00 기대, 실제 차감 500.00 → 초과 인출
    before = _state({"ATTACKER": "1000.00"})
    after = _state({"ATTACKER": "500.00"})
    codes = [200] * 8 + [400] * 2
    cand = _candidate()
    outcome = check_inv_1c(cand, _result(cand, before, after, codes))
    assert outcome.verdict == Verdict.VIOLATION
    assert outcome.evidence["n_success"] == 8


def test_inv_1c_passes_when_deduction_matches_successes():
    before = _state({"ATTACKER": "1000.00"})
    after = _state({"ATTACKER": "700.00"})  # 3건 성공 x 100 = 300 정확히 차감
    codes = [200, 200, 200, 400]
    cand = _candidate()
    assert check_inv_1c(cand, _result(cand, before, after, codes)).verdict == Verdict.PASS


def test_inv_1c_violation_on_overdraft_even_when_deduction_matches():
    """GT F-12 플레이버: 가용 잔액이 1건분(100)뿐인데 5건이 성공해 500 을 지출 —
    차감액 자체는 성공 건수와 정합(actual==expected==500)하지만, 시행 전 잔액을
    초과했으므로 잔액 검사가 경쟁조건으로 우회된 것이다. actual<expected 검사만
    으로는 못 잡는 경로를 result.before 로 잡는다."""
    before = _state({"ATTACKER": "100.00"})   # 딱 1건분
    after = _state({"ATTACKER": "-400.00"})   # 5건 성공 x 100 = 500 지출 → 음수
    codes = [200] * 5
    cand = _candidate()
    outcome = check_inv_1c(cand, _result(cand, before, after, codes))
    assert outcome.verdict == Verdict.VIOLATION
    assert "overdraft" in outcome.reason
    assert outcome.evidence["available_before"] == "100.00"


def test_inv_1c_passes_when_no_success():
    before = _state({"ATTACKER": "1000.00"})
    after = _state({"ATTACKER": "1000.00"})
    codes = [400, 400]
    cand = _candidate()
    assert check_inv_1c(cand, _result(cand, before, after, codes)).verdict == Verdict.PASS


def test_inv_1c_anomaly_on_double_deduction():
    before = _state({"ATTACKER": "1000.00"})
    after = _state({"ATTACKER": "400.00"})  # 성공 3건인데 600 차감 (기대 300보다 많음)
    codes = [200, 200, 200]
    cand = _candidate()
    outcome = check_inv_1c(cand, _result(cand, before, after, codes))
    assert outcome.verdict == Verdict.ANOMALY


def test_inv_1c_unknown_without_unit_amount():
    before = _state({"ATTACKER": "1000.00"})
    after = _state({"ATTACKER": "500.00"})
    cand = _candidate(unit_amount=None)
    outcome = check_inv_1c(cand, _result(cand, before, after, [200] * 5))
    assert outcome.verdict == Verdict.UNKNOWN


def test_inv_1c_unknown_on_missing_observation():
    before = ObservedState(principal_id="attacker_A", missing={"balance"})
    after = _state({"ATTACKER": "500.00"})
    cand = _candidate()
    outcome = check_inv_1c(cand, _result(cand, before, after, [200] * 5))
    assert outcome.verdict == Verdict.UNKNOWN
