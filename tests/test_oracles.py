"""oracles/(1A·1B·4·6) 단위 테스트. 전부 가짜 스냅샷만 쓴다 — 실제 서버 불필요."""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, ExpectedDelta
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Currency, Money
from finredteam.oracles.amount import check_inv_6
from finredteam.oracles.idempotency import check_inv_4
from finredteam.oracles.value import check_inv_1a, check_inv_1b

USD = Currency(code="USD", minor_unit=2)


def _state(principal_id: str, balances: dict[str, str]) -> ObservedState:
    return ObservedState(
        principal_id=principal_id,
        balances={k: Money.parse(v, USD) for k, v in balances.items()},
    )


def _result(candidate: CandidateFinding, before: ObservedState, after: ObservedState, codes: list[int]) -> ExecutionResult:
    delta = StateDelta.compute(before, after)
    return ExecutionResult(candidate=candidate, status_codes=codes, before=before, after=after, delta=delta)


def _candidate(**overrides) -> CandidateFinding:
    base = dict(
        id="c1",
        layer="finance",
        attack_class="F2",
        invariants=["INV-1B"],
        principal_id="attacker_A",
        method="POST",
        path="/transfer",
        subject_account="ATTACKER",
    )
    base.update(overrides)
    return CandidateFinding(**base)


# ── INV-1A: 가치 보존 (통화별) ──────────────────────────────────────────


def test_inv_1a_passes_when_currency_totals_conserved():
    before = _state("attacker_A", {"ATTACKER": "1000.00", "VICTIM": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "900.00", "VICTIM": "1100.00"})
    cand = _candidate()
    result = _result(cand, before, after, [200])

    outcome = check_inv_1a(cand, result)
    assert outcome.verdict == Verdict.PASS


def test_inv_1a_violation_when_value_created():
    # 정상 송금이 아니라 돈이 그냥 생겨난 경우 (양쪽 다 증가)
    before = _state("attacker_A", {"ATTACKER": "1000.00", "VICTIM": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "1000.00", "VICTIM": "1500.00"})
    cand = _candidate()
    result = _result(cand, before, after, [200])

    outcome = check_inv_1a(cand, result)
    assert outcome.verdict == Verdict.VIOLATION


def test_inv_1a_unknown_when_missing():
    before = ObservedState(principal_id="attacker_A", missing={"balance"})
    after = _state("attacker_A", {"ATTACKER": "900.00"})
    cand = _candidate()
    result = _result(cand, before, after, [200])

    outcome = check_inv_1a(cand, result)
    assert outcome.verdict == Verdict.UNKNOWN


def test_inv_1a_conserved_even_on_negative_transfer_sign_flip():
    # §18 예시: amount=-500 이면 방향은 반대지만 총합은 보존된다 — INV-1A 는 PASS.
    before = _state("attacker_A", {"ATTACKER": "1000.00", "VICTIM": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "1500.00", "VICTIM": "500.00"})
    cand = _candidate()
    result = _result(cand, before, after, [200])

    assert check_inv_1a(cand, result).verdict == Verdict.PASS


# ── INV-1B: 인가된 가치 흐름 (baseline 기준) ────────────────────────────


def test_inv_1b_violation_on_negative_transfer_sign_flip():
    before = _state("attacker_A", {"ATTACKER": "1000.00", "VICTIM": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "1500.00", "VICTIM": "500.00"})
    cand = _candidate(
        expected_delta=ExpectedDelta(
            deltas={
                "ATTACKER": Money.parse("-500.00", USD),
                "VICTIM": Money.parse("500.00", USD),
            },
            provenance="baseline_observed",
            baseline_ref="bl_transfer_01",
        )
    )
    result = _result(cand, before, after, [200])

    outcome = check_inv_1b(cand, result)
    assert outcome.verdict == Verdict.VIOLATION
    assert "ATTACKER" in outcome.reason


def test_inv_1b_passes_on_normal_transfer():
    before = _state("attacker_A", {"ATTACKER": "1000.00", "VICTIM": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "990.00", "VICTIM": "1010.00"})
    cand = _candidate(
        expected_delta=ExpectedDelta(
            deltas={
                "ATTACKER": Money.parse("-10.00", USD),
                "VICTIM": Money.parse("10.00", USD),
            },
            provenance="baseline_observed",
            baseline_ref="bl_transfer_01",
        )
    )
    result = _result(cand, before, after, [200])
    assert check_inv_1b(cand, result).verdict == Verdict.PASS


def test_inv_1b_unknown_without_expected_delta():
    before = _state("attacker_A", {"ATTACKER": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "900.00"})
    cand = _candidate(expected_delta=None)
    result = _result(cand, before, after, [200])
    assert check_inv_1b(cand, result).verdict == Verdict.UNKNOWN


def test_inv_1b_unknown_when_provenance_is_llm_inferred():
    # ★ §6, §18 조건 8 — llm_inferred 는 절대 판정 근거로 쓰지 않는다.
    before = _state("attacker_A", {"ATTACKER": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "1500.00"})
    cand = _candidate(
        expected_delta=ExpectedDelta(
            deltas={"ATTACKER": Money.parse("-500.00", USD)},
            provenance="llm_inferred",
            note="LLM 이 추측한 값 — 판정에 쓰면 안 됨",
        )
    )
    result = _result(cand, before, after, [200])
    outcome = check_inv_1b(cand, result)
    assert outcome.verdict == Verdict.UNKNOWN
    assert "llm_inferred" in outcome.reason


# ── INV-4: 멱등성 (논리 거래 식별자 기준) ───────────────────────────────


def test_inv_4_unknown_without_logical_txn_id():
    before = _state("m", {"MERCHANT": "0.00"})
    after = _state("m", {"MERCHANT": "10.00"})
    cand = _candidate(attack_class="F3", invariants=["INV-4"], logical_txn_id=None)
    result = _result(cand, before, after, [200])
    assert check_inv_4(cand, result).verdict == Verdict.UNKNOWN


def test_inv_4_passes_when_same_payload_different_logical_id_charged_thrice():
    # 같은 커피 3번 결제(서로 다른 logical_txn_id) 는 정상이다 — 이 오라클은 "단일
    # logical_txn_id 로 3번 성공 응답"인 경우만 위반으로 본다. 여기서는 실제 델타가
    # 1회분과 같다고 가정한 시나리오(예: 이 특정 시도 자체는 1회만 성공) 를 PASS 로 검증.
    before = _state("m", {"MERCHANT": "0.00"})
    after = _state("m", {"MERCHANT": "10.00"})
    cand = _candidate(
        attack_class="F3",
        invariants=["INV-4"],
        logical_txn_id="order-123",
        subject_account="MERCHANT",
        expected_delta=ExpectedDelta(
            deltas={"MERCHANT": Money.parse("10.00", USD)},
            provenance="baseline_observed",
        ),
    )
    result = _result(cand, before, after, [200])
    assert check_inv_4(cand, result).verdict == Verdict.PASS


def test_inv_4_violation_when_repeated_same_logical_id_multiplies_side_effect():
    before = _state("m", {"MERCHANT": "0.00"})
    after = _state("m", {"MERCHANT": "30.00"})  # 10.00 짜리 결제가 3번 다 반영됨
    cand = _candidate(
        attack_class="F3",
        invariants=["INV-4"],
        logical_txn_id="order-123",
        subject_account="MERCHANT",
        expected_delta=ExpectedDelta(
            deltas={"MERCHANT": Money.parse("10.00", USD)},
            provenance="baseline_observed",
        ),
    )
    result = _result(cand, before, after, [200, 200, 200])
    outcome = check_inv_4(cand, result)
    assert outcome.verdict == Verdict.VIOLATION
    assert outcome.evidence["n_success"] == 3


# ── INV-6: 금액 정합성 (정밀도·부호) ────────────────────────────────────


def test_inv_6_violation_on_sub_minor_unit_precision():
    before = _state("attacker_A", {"ATTACKER": "1000.00"})
    after = ObservedState(
        principal_id="attacker_A",
        balances={"ATTACKER": Money(amount=Decimal("899.999"), currency=USD)},
    )
    cand = _candidate()
    result = _result(cand, before, after, [200])
    outcome = check_inv_6(cand, result)
    assert outcome.verdict == Verdict.VIOLATION
    assert "정밀도" in outcome.reason


def test_inv_6_violation_on_sign_mismatch():
    before = _state("attacker_A", {"ATTACKER": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "1500.00"})  # 차감이 아니라 증가
    cand = _candidate(
        subject_account="ATTACKER",
        expected_delta=ExpectedDelta(
            deltas={"ATTACKER": Money.parse("-500.00", USD)},
            provenance="baseline_observed",
        ),
    )
    result = _result(cand, before, after, [200])
    outcome = check_inv_6(cand, result)
    assert outcome.verdict == Verdict.VIOLATION
    assert "부호" in outcome.reason


def test_inv_6_passes_on_clean_debit():
    before = _state("attacker_A", {"ATTACKER": "1000.00"})
    after = _state("attacker_A", {"ATTACKER": "990.00"})
    cand = _candidate(
        subject_account="ATTACKER",
        expected_delta=ExpectedDelta(
            deltas={"ATTACKER": Money.parse("-10.00", USD)},
            provenance="baseline_observed",
        ),
    )
    result = _result(cand, before, after, [200])
    assert check_inv_6(cand, result).verdict == Verdict.PASS


# ── §8 규칙 6: oracles/ 는 LLM·네트워크를 호출하지 않는다 (순수 함수) ─────

_FORBIDDEN_IMPORT_ROOTS = ("httpx", "anthropic", "openai", "finredteam.llm", "finredteam.probing")


def test_oracles_are_pure_no_llm_or_network_imports():
    oracles_dir = Path(__file__).resolve().parent.parent / "finredteam" / "oracles"
    offenders: list[str] = []
    for path in sorted(oracles_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == root or name.startswith(root + ".") for root in _FORBIDDEN_IMPORT_ROOTS):
                    offenders.append(f"{path.name}: import {name}")
    assert not offenders, f"oracles/ 는 순수 함수여야 한다 — 금지된 import 발견: {offenders}"
