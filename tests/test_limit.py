"""INV-5(한도/정책 검증) 단위 테스트 — 전부 가짜 policy_signals 만 쓴다(서버 불필요).

양수성/최소/최대/기기한도-미적용 4종과, 거부 시 PASS·신호 없을 때 UNKNOWN 을 검사한다.
"""
from __future__ import annotations

from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.limit import check_inv_5


def _cand() -> CandidateFinding:
    return CandidateFinding(
        id="c1", layer="finance", operation="request_loan",
        principal_id="attacker_A", method="POST", path="/request_loan",
    )


def _result(**policy_signals) -> ExecutionResult:
    return ExecutionResult(candidate=_cand(), status_codes=[200], policy_signals=policy_signals)


def test_unknown_without_signals():
    assert check_inv_5(_cand(), ExecutionResult(candidate=_cand())).verdict == Verdict.UNKNOWN


def test_unknown_without_amount():
    assert check_inv_5(_cand(), _result(accepted=True)).verdict == Verdict.UNKNOWN


def test_rejected_request_passes():
    # 음수여도 서버가 거부했으면 정책 준수 → PASS
    out = check_inv_5(_cand(), _result(amount="-500.00", accepted=False, operation_label="loan"))
    assert out.verdict == Verdict.PASS


def test_negative_amount_accepted_is_violation():
    out = check_inv_5(_cand(), _result(amount="-500.00", accepted=True, operation_label="loan", currency="USD"))
    assert out.verdict == Verdict.VIOLATION
    assert "양수성" in out.reason


def test_zero_amount_accepted_is_violation():
    out = check_inv_5(_cand(), _result(amount="0", accepted=True, operation_label="bill"))
    assert out.verdict == Verdict.VIOLATION


def test_below_minimum_is_violation():
    out = check_inv_5(_cand(), _result(amount="0.50", min="1.00", accepted=True, operation_label="bill"))
    assert out.verdict == Verdict.VIOLATION
    assert "최소" in out.reason


def test_above_maximum_is_violation():
    out = check_inv_5(_cand(), _result(amount="1000000.00", max="10000.00", accepted=True, operation_label="loan"))
    assert out.verdict == Verdict.VIOLATION
    assert "최대" in out.reason


def test_enforced_limit_not_applied_is_violation():
    # 카드 한도 500 인데 사용액 800 이 허용됨 (F-19)
    out = check_inv_5(_cand(), _result(amount="800.00", enforced_limit="500.00", usage_after="800.00", accepted=True, operation_label="card_limit"))
    assert out.verdict == Verdict.VIOLATION
    assert "한도 미적용" in out.reason


def test_within_policy_passes():
    out = check_inv_5(_cand(), _result(amount="100.00", min="1.00", max="10000.00", enforced_limit="500.00", usage_after="100.00", accepted=True, operation_label="loan"))
    assert out.verdict == Verdict.PASS


def test_registered_in_registry_and_runs_in_run_all():
    from finredteam.oracles import registry
    assert "INV-5" in registry.REGISTRY
    outcomes = registry.run_all(_cand(), _result(amount="-1", accepted=True))
    assert any(o.invariant == "INV-5" and o.verdict == Verdict.VIOLATION for o in outcomes)
