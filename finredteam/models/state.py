"""관측 상태 — ObservedState, Snapshot, StateDelta.

모든 금액 필드는 `Money` 다 (§4 규칙 1). 관측 불가능한 필드는 값을 만들어내지 않고
`missing` 집합에 이름을 남긴다 — 오라클이 이를 보고 `UNKNOWN`을 낸다(§4 규칙 5).
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from finredteam.money import Money


class ObservedState(BaseModel):
    """특정 시점, 특정 주체 관점에서 관측한 상태 스냅샷."""

    principal_id: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    balances: dict[str, Money] = {}  # key: account_number (또는 card_id 등 잔액 보유 단위)
    cards: dict[str, Money] = {}  # key: card_id -> 카드 잔액
    missing: set[str] = set()  # 관측하려 했으나 실패한 필드/키 이름

    def has_missing(self) -> bool:
        return len(self.missing) > 0


class Snapshot(BaseModel):
    """StateDelta 계산의 재료가 되는 전/후 스냅샷 쌍."""

    before: ObservedState
    after: ObservedState


class StateDelta(BaseModel):
    """전/후 스냅샷의 차이. balance_delta 는 계정 단위로 Money 를 담는다."""

    before: ObservedState
    after: ObservedState
    balance_delta: dict[str, Money] = {}
    missing: set[str] = set()

    def has_missing(self) -> bool:
        return self.before.has_missing() or self.after.has_missing() or bool(self.missing)

    @classmethod
    def compute(cls, before: ObservedState, after: ObservedState) -> "StateDelta":
        delta: dict[str, Money] = {}
        missing: set[str] = set(before.missing) | set(after.missing)
        keys = set(before.balances) | set(after.balances)
        for key in keys:
            b = before.balances.get(key)
            a = after.balances.get(key)
            if b is None or a is None:
                missing.add(key)
                continue
            delta[key] = a - b
        return cls(before=before, after=after, balance_delta=delta, missing=missing)
