from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# ══════════════════════════════════════════════════════════════════════
#  1. 값 타입 — 금액과 출처
# ══════════════════════════════════════════════════════════════════════


class MoneyValue(BaseModel):
    amount: Decimal
    currency: str                 # ISO 4217 ("USD", "KRW") 또는 대상 고유 코드
    minor_unit: int = 2           # USD=2, KRW=0, BTC=8

    model_config = {"frozen": True}

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, v):
        if isinstance(v, float):
            raise TypeError(
                f"금액에 float 을 쓸 수 없다({v!r}) — 문자열이나 Decimal 로 넘길 것"
            )
        return Decimal(str(v)) if not isinstance(v, Decimal) else v

    def tolerance(self) -> Decimal:
        return Decimal(1).scaleb(-self.minor_unit)

    def same_currency(self, other: "MoneyValue") -> bool:
        return self.currency == other.currency

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


Confidence = Literal[
    "measured",         # 우리가 직접 관측했다 (응답 본문에서 읽음) — 가장 강함
    "server_declared",  # 서버가 스스로 선언한 정책값 (biller 최소금액 등) — 강함
    "asserted",         # 우리가 도메인 상식으로 가정한 값 — 약함, 단독 근거 금지
    "inferred",         # 다른 값에서 추론 — 가장 약함, 참고용
]


class Provenance(BaseModel):
    step: int
    tool_id: str
    evidence_ref: str             # ★ 빈 문자열 금지 — 아래 validator 가 막는다
    # ★ 기본값은 **가장 약한 등급**이다. 강한 등급이 기본이면 작성자가 깜빡한 값이
    #   자동으로 "우리가 직접 관측했다" 가 되어, 판정의 단독 근거로 승격된다.
    #   증거 등급은 올릴 때 의식적으로 올려야 한다.
    confidence: Confidence = "inferred"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("evidence_ref")
    @classmethod
    def _require_evidence(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence_ref 없이 관측값을 기록할 수 없다")
        return v

    @property
    def is_strong(self) -> bool:
        return self.confidence in ("measured", "server_declared")


class MoneyObservation(BaseModel):
    value: MoneyValue
    provenance: Provenance


class ScalarObservation(BaseModel):
    value: Union[str, int, bool]
    provenance: Provenance


# ══════════════════════════════════════════════════════════════════════
#  2. 정책 경계 — 금융 레드팀의 핵심 구분
# ══════════════════════════════════════════════════════════════════════


class PolicyBound(BaseModel):
    field: str                              # "amount", "card_limit", "loan_amount"
    minimum: Optional[MoneyValue] = None
    maximum: Optional[MoneyValue] = None
    declared_by: Literal["server", "probe_assertion", "engagement"]
    provenance: Provenance

    def violates(self, attempted: MoneyValue) -> Optional[str]:
        if self.minimum and attempted.same_currency(self.minimum) and attempted.amount < self.minimum.amount:
            return f"{attempted} < 최소 {self.minimum} ({self.declared_by} 선언)"
        if self.maximum and attempted.same_currency(self.maximum) and attempted.amount > self.maximum.amount:
            return f"{attempted} > 최대 {self.maximum} ({self.declared_by} 선언)"
        return None

    @property
    def evidence_strength(self) -> Literal["strong", "weak"]:
        return "strong" if self.declared_by == "server" else "weak"


# ══════════════════════════════════════════════════════════════════════
#  3. 노드별 사실 — kind 마다 다른 형태
# ══════════════════════════════════════════════════════════════════════


class _BalanceHistoryMixin(BaseModel):
    balance_history: list[MoneyObservation] = []

    def latest_balance(self) -> Optional[MoneyObservation]:
        return max(self.balance_history, key=lambda o: o.provenance.step, default=None)

    def balance_at(self, step: int) -> Optional[MoneyObservation]:
        prior = [o for o in self.balance_history if o.provenance.step <= step]
        return max(prior, key=lambda o: o.provenance.step, default=None)

    def absorb_history(self, other: "_BalanceHistoryMixin") -> None:
        seen = {(o.provenance.step, o.provenance.evidence_ref, str(o.value))
                for o in self.balance_history}
        for o in other.balance_history:
            key = (o.provenance.step, o.provenance.evidence_ref, str(o.value))
            if key not in seen:
                self.balance_history.append(o)
                seen.add(key)
        self.balance_history.sort(key=lambda o: o.provenance.step)

    def delta(self, before_step: int, after_step: int) -> Optional[MoneyValue]:
        b, a = self.balance_at(before_step), self.balance_at(after_step)
        if b is None or a is None or not b.value.same_currency(a.value):
            return None
        return MoneyValue(
            amount=a.value.amount - b.value.amount,
            currency=a.value.currency,
            minor_unit=a.value.minor_unit,
        )


class AccountFacts(_BalanceHistoryMixin):
    account_number: str
    currency: str
    account_type: Optional[str] = None          # "checking", "savings" ...
    owner_principal: Optional[str] = None       # principal 노드 id. 모르면 None
    # ★ 소유자를 모른다는 것 자체가 정보다 — oracles/authz 가 UNKNOWN 을 내는 근거
    overdraft_allowed: Optional[ScalarObservation] = None
    policy_bounds: list[PolicyBound] = []


_PAN_LIKE = re.compile(r"^\d{9,}$")


class CardFacts(_BalanceHistoryMixin):
    card_id: str
    owner_principal: Optional[str] = None
    currency: str
    declared_limit: Optional[MoneyObservation] = None   # 서버가 되돌려준 한도
    status: Literal["active", "frozen", "unknown"] = "unknown"
    policy_bounds: list[PolicyBound] = []

    # 노출 신호 — 값이 아니라 사실만
    pan_fully_exposed: bool = False
    cvv_exposed: bool = False
    pan_last4: Optional[str] = None

    @field_validator("pan_last4")
    @classmethod
    def _only_last4(cls, v):
        if v is None:
            return v
        if _PAN_LIKE.match(v) or len(v) > 4:
            raise ValueError(
                "pan_last4 에 전체 PAN 을 넣을 수 없다 — 뒷 4자리만 허용"
            )
        return v


class PrincipalFacts(BaseModel):
    username: Optional[str] = None
    owned_accounts: list[str] = []              # account 노드 id
    role_claimed: list[str] = []                # 토큰 클레임 등 — 주장
    role_verified: list[ScalarObservation] = [] # 실제로 통한 것 — 관측
    token_node: Optional[str] = None
    is_self_created: bool = False               # 우리가 만든 테스트 계정인가(정리 대상)
    # ★ 비밀번호는 어떤 형태로도 저장하지 않는다 — 필드 자체를 두지 않는다


class TokenFacts(BaseModel):
    token_ref: str                              # evidence store 안의 참조
    alg: Optional[str] = None                   # "HS256", "none"
    has_expiry: Optional[bool] = None
    claims_seen: list[str] = []                 # 클레임 **이름**만. 값은 안 담는다
    forged: bool = False
    forged_with: Optional[str] = None           # 약한 시크릿이 뚫렸을 때 그 사실
    accepted_by_server: Optional[bool] = None


class BillerFacts(BaseModel):
    biller_id: str
    name: Optional[str] = None
    category_id: Optional[str] = None
    policy_bounds: list[PolicyBound] = []       # declared_by="server" 인 것들

    def server_bound(self, field: str) -> Optional[PolicyBound]:
        return next(
            (b for b in self.policy_bounds if b.field == field and b.declared_by == "server"),
            None,
        )


class EndpointFacts(BaseModel):
    method: str
    path: str                                   # "{id}" 로 정규화된 형태
    requires_auth: Optional[bool] = None
    observed_status: list[int] = []
    response_fields: list[str] = []             # 응답 키 **이름**만 (값 저장 금지)
    sensitivity: Literal["none", "low", "high", "critical"] = "none"
    discovered_via: str = ""                    # "seed" | "link_extraction" | "tool:probe_path"


class LoanFacts(BaseModel):
    loan_id: Optional[str] = None
    requested: Optional[MoneyObservation] = None
    status: Optional[ScalarObservation] = None
    approvals_seen: int = 0                     # 중복 승인(INV-4) 판정 재료
    policy_bounds: list[PolicyBound] = []


class HostFacts(BaseModel):
    # 그림의 Asset(호스트) — endpoint 는 경로만 담아서 호스트를 적을 곳이 없었다.
    hostname: str
    ip: Optional[str] = None
    in_scope: Optional[bool] = None             # 승인 범위 안인가
    tech_fingerprint: list[str] = []            # 값이 아니라 식별된 이름만


class AppFacts(BaseModel):
    # 그림의 Asset(APK) — 모바일 대상 앱 자체.
    package: str
    version_name: Optional[str] = None
    platform: Literal["android", "ios", "unknown"] = "unknown"
    # ★ 여기에도 값은 담지 않는다. APK 에서 키를 발견했으면 "발견했다" 만 남기고
    #   값 자체는 증거 저장소로 간다 — 그래프는 오래 남는다.
    hardcoded_secret_found: bool = False
    debuggable: Optional[bool] = None
    discovered_via: str = ""


class ScreenFacts(BaseModel):
    # 모바일에서 EndpointFacts 에 해당한다. 앱 화면이 곧 요청 표면이다.
    package: str
    screen: str                                 # 액티비티/화면 이름
    requires_auth: Optional[bool] = None
    observed_actions: list[str] = []            # 실제로 통한 동작 이름만
    input_fields: list[str] = []                # 필드 **이름**만 (값 저장 금지)
    sensitivity: Literal["none", "low", "high", "critical"] = "none"
    discovered_via: str = ""


NodeFacts = Union[
    AccountFacts, CardFacts, PrincipalFacts, TokenFacts,
    BillerFacts, EndpointFacts, LoanFacts,
    HostFacts, AppFacts, ScreenFacts,
]


def merge_facts(old: NodeFacts, new: NodeFacts) -> NodeFacts:
    if old is None:
        return new
    if new is None or type(old) is not type(new):
        return new if new is not None else old

    merged = old.model_copy(deep=True)
    if isinstance(merged, _BalanceHistoryMixin):
        merged.absorb_history(new)

    for name in type(new).model_fields:
        if name == "balance_history":
            continue                      # 위에서 이미 처리
        nv, ov = getattr(new, name), getattr(merged, name)
        if nv is None:
            continue
        if isinstance(ov, list):
            for item in nv:
                if item not in ov:
                    ov.append(item)
        elif isinstance(ov, bool) and isinstance(nv, bool):
            setattr(merged, name, ov or nv)          # sticky True
        else:
            setattr(merged, name, nv)
    return merged


# ══════════════════════════════════════════════════════════════════════
#  4. 가치 이동 — 금융 레드팀의 판정 원재료
# ══════════════════════════════════════════════════════════════════════


class ValueLeg(BaseModel):
    node_id: str                                # account:... 또는 card:...
    delta: MoneyValue
    provenance: Provenance


class ValueFlow(BaseModel):
    transition_id: str
    legs: list[ValueLeg] = []
    # ★ 기본값은 **개방계**다. 폐쇄계(closed=True)라고 말하는 순간 "순합이 0이
    #   아니면 위반" 이 되므로, 관측을 덜 한 상태가 곧 VIOLATION 으로 둔갑한다.
    #   "이 전이의 가치 이동을 빠짐없이 봤다" 는 **주장**이고, 주장은 명시해야 한다.
    # closed=False: 관측 밖으로 값이 정상적으로 흘러나가는 경우(가맹점 정산 등).
    # 이때 보존 여부는 **판정 불가**다 — 억지로 VIOLATION 을 내면 오탐이 된다.
    closed: bool = False

    def net(self) -> dict[str, MoneyValue]:
        totals: dict[str, Decimal] = {}
        units: dict[str, int] = {}
        for leg in self.legs:
            cur = leg.delta.currency
            totals[cur] = totals.get(cur, Decimal(0)) + leg.delta.amount
            units[cur] = max(units.get(cur, 0), leg.delta.minor_unit)
        return {
            cur: MoneyValue(amount=amt, currency=cur, minor_unit=units[cur])
            for cur, amt in totals.items()
        }

    def is_conserved(self) -> Optional[bool]:
        if not self.closed or not self.legs:
            return None
        return all(abs(m.amount) < m.tolerance() for m in self.net().values())

    def beneficiaries(self) -> list[ValueLeg]:
        return [l for l in self.legs if l.delta.amount > l.delta.tolerance()]

    def sources(self) -> list[ValueLeg]:
        return [l for l in self.legs if l.delta.amount < -l.delta.tolerance()]

    def explain(self) -> str:
        net = ", ".join(f"{c}: {m.amount}" for c, m in sorted(self.net().items()))
        verdict = {True: "보존됨", False: "보존 위반", None: "판정 불가"}[self.is_conserved()]
        return (
            f"{len(self.legs)}개 객체, 통화별 순합 [{net}] → {verdict}"
            + ("" if self.closed else " (개방계 — 관측 밖 정산 상대 존재)")
        )
