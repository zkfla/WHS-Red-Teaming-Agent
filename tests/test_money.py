from decimal import Decimal

import pytest

from finredteam.money import Currency, CurrencyMismatch, Money, loads_money_json, tol

USD = Currency(code="USD", minor_unit=2)
KRW = Currency(code="KRW", minor_unit=0)
BTC = Currency(code="BTC", minor_unit=8)


def test_decimal_precision_not_float():
    # float 로는 0.1 + 0.2 != 0.3 이지만 Money 는 Decimal 이라 정확히 일치한다.
    a = Money.parse(0.1, USD)
    b = Money.parse(0.2, USD)
    assert (a + b).amount == Decimal("0.3")


def test_money_amount_rejects_float_directly():
    with pytest.raises(TypeError):
        Money(amount=0.1, currency=USD)


def test_parse_from_string_and_number():
    assert Money.parse("10.50", USD).amount == Decimal("10.50")
    assert Money.parse(10, USD).amount == Decimal("10")


def test_currency_mismatch_raises():
    usd = Money.parse("10", USD)
    krw = Money.parse("10", KRW)
    with pytest.raises(CurrencyMismatch):
        usd + krw
    with pytest.raises(CurrencyMismatch):
        usd - krw
    with pytest.raises(CurrencyMismatch):
        usd < krw


def test_negative_transfer_sign_flip_reproduction():
    # §18 예시: amount=-500 송금 시 attacker +500, victim -500 로 부호가 반전된다.
    attacker_before = Money.parse("1000.00", USD)
    victim_before = Money.parse("1000.00", USD)
    amount = Money.parse("-500.00", USD)

    attacker_after = attacker_before - amount  # balance -= amount
    victim_after = victim_before + amount  # balance += amount

    assert attacker_after.amount == Decimal("1500.00")
    assert victim_after.amount == Decimal("500.00")

    # INV-1A(가치 보존)는 지켜진다 — 총합 불변
    assert (attacker_before + victim_before).amount == (attacker_after + victim_after).amount
    # 하지만 INV-1B(인가된 가치 흐름)는 위반된다 — 방향이 반대
    delta_attacker = attacker_after - attacker_before
    assert delta_attacker.amount > 0  # 보내는 사람 잔액이 오히려 증가


def test_quantize_rounds_half_up():
    m = Money(amount=Decimal("10.005"), currency=USD)
    assert m.quantize().amount == Decimal("10.01")

    krw = Money(amount=Decimal("100.6"), currency=KRW)
    assert krw.quantize().amount == Decimal("101")


def test_tol_uses_currency_minor_unit_not_hardcoded():
    assert tol(USD) == Decimal("0.01")
    assert tol(KRW) == Decimal("1")
    assert tol(BTC) == Decimal("0.00000001")


def test_mul_by_int_for_concurrency_expected_delta():
    unit = Money.parse("100.00", USD)
    n_success = 8
    assert (unit * n_success).amount == Decimal("800.00")


def test_mul_by_float_rejected():
    unit = Money.parse("100.00", USD)
    with pytest.raises(TypeError):
        unit * 1.5  # type: ignore[operator]


def test_loads_money_json_uses_decimal_not_float():
    parsed = loads_money_json('{"balance": 900.10, "n": 3}')
    assert isinstance(parsed["balance"], Decimal)
    assert parsed["balance"] == Decimal("900.10")


def test_abs_and_neg_preserve_currency():
    m = Money.parse("-42.50", USD)
    assert abs(m).amount == Decimal("42.50")
    assert (-m).amount == Decimal("42.50")
    assert abs(m).currency == USD
