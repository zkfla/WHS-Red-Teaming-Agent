"""카드 충전(card fund) 환율 조작(Mass Assignment) 시험 오케스트레이션.

실제 소스(app.py::fund_virtual_card)는 "Mass assignment allows client input to
override internal funding fields such as the exchange rate" — 클라이언트가
`exchange_rate` 를 요청 바디에 직접 넣으면 서버 기본 환율을 덮어쓴다.

USD 카드에 대해 이 조작을 하면: 계좌에서 usd_amount 만큼 빠지고, 카드에는
usd_amount × 조작환율 만큼 들어온다 — 둘 다 USD 이므로 같은 통화에서 값이
창조된다. 그래서 계좌 잔액과 카드 잔액을 한 델타로 합쳐 관측하면
`oracles/value.py::check_inv_1a`(통화별 가치 보존)가 새 오라클 없이 그대로
위반을 잡는다.

★ 이 시험은 "카드 소유자 자신"이 자기 계좌·카드로 수행한다(BOLA 가 아니라
가치 무결성 문제). 소유자만 아는 카드 잔액 관측이 필요하므로 계좌 관측
(observation.balance)과 카드 관측(get_own_cards)을 둘 다 쓴다.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Currency, Money
from finredteam.probing.session import Session

_USD = Currency(code="USD", minor_unit=2)


def _account_balance(session: Session, account_number: str) -> Money | None:
    try:
        ex = session.anonymous_request("GET", f"/check_balance/{account_number}")
    except Exception:
        return None
    if ex.status_code >= 400 or not isinstance(ex.response_body, dict):
        return None
    raw = ex.response_body.get("balance")
    try:
        return Money.parse(raw, _USD)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _card_balance(session: Session, owner: Principal, card_id: str) -> Money | None:
    for c in session.get_own_cards(owner):
        if str(c.get("id")) == str(card_id):
            try:
                return Money.parse(c.get("balance"), _USD)
            except (InvalidOperation, ValueError, TypeError):
                return None
    return None


def run_card_fund_manipulation(
    session: Session,
    owner: Principal,
    card_id: str,
    *,
    usd_amount: str = "100.00",
    exchange_rate: str = "100",
) -> tuple[CandidateFinding, ExecutionResult]:
    """USD 카드를 `exchange_rate` 를 조작해 충전하고, 계좌+카드 잔액 변화를 한
    델타로 합쳐 CandidateFinding/ExecutionResult 를 만든다. 판정은 호출부가
    `oracles/verifier.py::verify()` 로 별도 실행한다 — 여기서는 재료만 만든다.

    정상 충전(exchange_rate 미지정, USD)이라면 계좌 -amount / 카드 +amount 로
    USD 총합이 0 이라 INV-1A 는 통과한다. 조작하면 총합이 0 이 아니어서 위반.
    """
    assert owner.account_number is not None
    acct = owner.account_number

    acct_before = _account_balance(session, acct)
    card_before = _card_balance(session, owner, card_id)

    ex = session.authed_request(
        "POST",
        f"/api/virtual-cards/{card_id}/fund",
        owner,
        json_body={"amount": usd_amount, "exchange_rate": exchange_rate},
    )

    acct_after = _account_balance(session, acct)
    card_after = _card_balance(session, owner, card_id)

    card_key = f"card:{card_id}"
    before_state = ObservedState(principal_id=owner.id)
    after_state = ObservedState(principal_id=owner.id)
    balance_delta: dict[str, Money] = {}
    missing: set[str] = set()

    if acct_before is not None and acct_after is not None:
        before_state.balances[acct] = acct_before
        after_state.balances[acct] = acct_after
        balance_delta[acct] = acct_after - acct_before
    else:
        missing.add(f"account:{acct}:balance")

    if card_before is not None and card_after is not None:
        before_state.cards[card_key] = card_before
        after_state.cards[card_key] = card_after
        balance_delta[card_key] = card_after - card_before
    else:
        missing.add(f"{card_key}:balance")

    candidate = CandidateFinding(
        id=f"cardfund-{card_id}",
        layer="finance",
        operation="card_fund",
        principal_id=owner.id,
        method="POST",
        path=f"/api/virtual-cards/{card_id}/fund",
        params={"amount": usd_amount, "exchange_rate": exchange_rate},
        subject_account=card_key,
        watch_accounts=[acct, card_key],
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[ex.status_code],
        before=before_state,
        after=after_state,
        delta=StateDelta(before=before_state, after=after_state, balance_delta=balance_delta, missing=missing),
    )
    return candidate, result
