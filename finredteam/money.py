"""금액 표현 — ★ float 금지 (instruction.md §4).

`0.1 + 0.2 != 0.3` 오차가 그대로 INV-1A(가치 보존) 오탐이 되므로, 금액이 지나가는
모든 경로에서 `float`를 절대 경유하지 않는다. API 응답의 JSON 파싱에도
`loads_money_json`(parse_float=Decimal)을 쓴다 — 표준 `json.loads`/`response.json()`을
직접 쓰지 않는다.
"""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, field_validator


class CurrencyMismatch(Exception):
    """서로 다른 통화끼리 산술을 시도했을 때."""


class Currency(BaseModel):
    code: str  # "USD", "KRW", "BTC" ...
    minor_unit: int  # USD=2, KRW=0, BTC=8

    model_config = {"frozen": True}

    def __hash__(self) -> int:
        return hash((self.code, self.minor_unit))


class Money(BaseModel):
    amount: Decimal
    currency: Currency

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> Decimal:
        # float 가 여기로 들어오면 이미 정밀도가 손상된 뒤이므로, 상류에서 막아야 한다.
        # 그래도 str()을 경유해 추가 손상만은 방지한다.
        if isinstance(v, float):
            raise TypeError(
                f"Money.amount 에 float 전달 금지: {v!r} — Money.parse() 또는 "
                f"Decimal(str(x))를 사용하라"
            )
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @classmethod
    def parse(cls, raw: Any, currency: Currency) -> "Money":
        """API 응답값을 Decimal 로 변환한다. float 를 절대 경유하지 마라."""
        if isinstance(raw, float):
            raw = str(raw)
        return cls(amount=Decimal(str(raw)), currency=currency)

    def _assert_same_currency(self, other: "Money") -> None:
        if self.currency.code != other.currency.code:
            raise CurrencyMismatch(
                f"통화 불일치: {self.currency.code} vs {other.currency.code}"
            )

    def __add__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __neg__(self) -> "Money":
        return Money(amount=-self.amount, currency=self.currency)

    def __abs__(self) -> "Money":
        return Money(amount=abs(self.amount), currency=self.currency)

    def __mul__(self, factor: int | Decimal) -> "Money":
        # int: 단위금액 × 성공건수(concurrency). Decimal: baseline 비례 스케일링(baseline.py).
        # float 는 정밀도를 이미 잃은 뒤이므로 명시적으로 거부한다.
        if isinstance(factor, bool) or not isinstance(factor, (int, Decimal)):
            raise TypeError(
                f"Money 곱셈은 int 또는 Decimal 배수만 허용한다: {factor!r}"
            )
        return Money(amount=self.amount * factor, currency=self.currency)

    __rmul__ = __mul__

    def _cmp_check(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise TypeError(f"Money 는 Money 와만 비교 가능: {other!r}")
        self._assert_same_currency(other)

    def __lt__(self, other: "Money") -> bool:
        self._cmp_check(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._cmp_check(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._cmp_check(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._cmp_check(other)
        return self.amount >= other.amount

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.currency.code == other.currency.code and self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.amount, self.currency))

    def quantize(self) -> "Money":
        exp = Decimal(1).scaleb(-self.currency.minor_unit)
        return Money(
            amount=self.amount.quantize(exp, rounding=ROUND_HALF_UP),
            currency=self.currency,
        )

    def __str__(self) -> str:
        return f"{self.amount.quantize(Decimal(1).scaleb(-self.currency.minor_unit))} {self.currency.code}"


def tol(currency: Currency) -> Decimal:
    """통화의 최소 단위를 비교 허용오차로 쓴다. 0.01 같은 상수를 하드코딩하지 않는다."""
    return Decimal(1).scaleb(-currency.minor_unit)


def loads_money_json(text: str) -> Any:
    """금액이 포함된 JSON 응답을 float 경유 없이 파싱한다."""
    return json.loads(text, parse_float=Decimal)
