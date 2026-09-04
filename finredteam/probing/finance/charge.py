"""가맹점 결제(merchant charge) 멱등성(INV-4) 시험 오케스트레이션.

`probing/finance/burst.py`(F4/INV-1C)와 같은 이유로 일반 `probing/executor.py`
를 안 쓴다 — merchant charge 는 계좌가 아니라 카드 잔액을 관측해야 하고
(`Session.get_own_cards`), 인증 주체도 일반 user 가 아니라 별도 merchant
토큰이다. 같은 `merchant_order_id` 로 N 번 결제를 반복하고, 카드 잔액 변화가
"1회분"인지("멱등하게 막힘") "N회분"인지("멱등성 없음, 위반")를 오라클
(`oracles/idempotency.py::check_inv_4`)이 판정하게 재료를 만든다.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from finredteam.models.finding import CandidateFinding, ExecutionResult, ExpectedDelta
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Currency, Money
from finredteam.probing.session import Session


def _find_card_balance(cards: list[dict], card_id: str) -> Money | None:
    currency = Currency(code="USD", minor_unit=2)  # vuln-bank 카드 기본 통화(§ vulnbank.yaml)
    for c in cards:
        if str(c.get("id")) == str(card_id):
            try:
                return Money.parse(c.get("balance"), currency)
            except (InvalidOperation, ValueError):
                return None
    return None


def run_charge_idempotency_trial(
    session: Session,
    owner_principal,
    card: dict,
    merchant_token: str,
    order_id: str,
    amount: str,
    *,
    repeats: int = 3,
) -> tuple[CandidateFinding, ExecutionResult]:
    """같은 `order_id`로 `repeats`번 결제를 반복 실행하고, 카드 잔액 전/후를
    관측해 CandidateFinding + ExecutionResult 를 만든다. 판정(oracle)은 호출부가
    `oracles/verifier.py::verify()` 로 별도 실행한다 — 여기서는 재료만 만든다.
    """
    card_id = str(card["id"])
    before_cards = session.get_own_cards(owner_principal)
    before_balance = _find_card_balance(before_cards, card_id)

    status_codes: list[int] = []
    for _ in range(repeats):
        ex = session.raw_token_request(
            "POST",
            "/api/v1/payments/charge",
            merchant_token,
            json_body={
                "amount": amount,
                "currency": "USD",
                "card_number": card["card_number"],
                "cvv": card["cvv"],
                "expiry_date": card["expiry_date"],
                "merchant_order_id": order_id,
                "description": "idempotency-trial",
            },
        )
        status_codes.append(ex.status_code)

    after_cards = session.get_own_cards(owner_principal)
    after_balance = _find_card_balance(after_cards, card_id)

    currency = Currency(code="USD", minor_unit=2)
    before_state = ObservedState(
        principal_id=owner_principal.id, cards={card_id: before_balance} if before_balance is not None else {}
    )
    after_state = ObservedState(
        principal_id=owner_principal.id, cards={card_id: after_balance} if after_balance is not None else {}
    )
    missing = set()
    balance_delta: dict[str, Money] = {}
    if before_balance is not None and after_balance is not None:
        balance_delta[card_id] = after_balance - before_balance
    else:
        missing.add(f"card:{card_id}:balance")

    candidate = CandidateFinding(
        id=f"charge-{order_id}",
        layer="finance",
        operation="merchant_charge",
        principal_id=owner_principal.id,
        method="POST",
        path="/api/v1/payments/charge",
        params={"amount": amount, "merchant_order_id": order_id},
        logical_txn_id=order_id,
        expected_delta=ExpectedDelta(
            deltas={card_id: -Money.parse(amount, currency)},
            provenance="baseline_observed",
            baseline_ref="bl_charge_single_unit",
            note="정상 결제 1회는 카드 잔액에서 amount 만큼만 차감돼야 한다 — 이 값 자체가 "
            "'정상'의 정의(§6 provenance)이지 LLM 추측이 아니다.",
        ),
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=status_codes,
        before=before_state,
        after=after_state,
        delta=StateDelta(before=before_state, after=after_state, balance_delta=balance_delta, missing=missing),
    )
    return candidate, result
