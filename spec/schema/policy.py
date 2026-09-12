from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from spec.schema.finance import MoneyValue
from spec.schema.tool_manifest import Capability, ToolManifest

POLICY_VERSION = 1


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    rule: str = ""            # "RACE_TESTING_FORBIDDEN", "AMOUNT_OVER_LIMIT", ...
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


_OK = PolicyDecision(allowed=True)


@runtime_checkable
class PolicyPort(Protocol):
    def allows_capability(self, capability: str) -> PolicyDecision: ...
    def check_concurrency(self, concurrency: int) -> PolicyDecision: ...
    def check_amount(self, amount: MoneyValue) -> PolicyDecision: ...
    def check_surface(self, method: str, path: str) -> PolicyDecision: ...
    def check_budget(self, requests: int, duration_s: float) -> PolicyDecision: ...
    def policy_hash(self) -> str: ...


# ── capability → StaticPolicy 필드 ───────────────────────────────────────
#
# ★ 이 표가 곧 "모든 capability 는 검사된다" 는 약속이다. 아래 단언이 그 약속을
#   import 시점에 강제한다.
CAPABILITY_FLAGS: dict[str, str] = {
    "state_change":     "allow_state_change",
    "account_creation": "allow_account_creation",
    "file_upload":      "allow_file_upload",
    "delete":           "allow_delete",
    "race_testing":     "allow_race_testing",
    "dos":              "allow_dos",
    "bruteforce":       "allow_bruteforce",
    "external_service": "allow_external_service",
}

_CAPABILITY_VOCAB = frozenset(getattr(Capability, "__args__", ()))
assert frozenset(CAPABILITY_FLAGS) == _CAPABILITY_VOCAB, (
    "capability 어휘와 정책 검사 표가 어긋났다 — 검사되지 않는 권한이 생긴다: "
    f"{sorted(_CAPABILITY_VOCAB ^ frozenset(CAPABILITY_FLAGS))}"
)


class StaticPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: str = ""

    allow_state_change: bool = False
    allow_account_creation: bool = False
    allow_file_upload: bool = False
    allow_delete: bool = False
    allow_race_testing: bool = False
    allow_dos: bool = False
    allow_bruteforce: bool = False
    allow_external_service: bool = False

    max_concurrency: int = 1
    max_amount: MoneyValue | None = None        # None = 금액을 건드리는 tool 전면 금지
    max_requests_per_step: int = 50
    max_duration_s: float = 60.0

    allowed_path_prefixes: list[str] = ["/"]    # 승인 범위 (경로 기준)
    denied_path_prefixes: list[str] = []        # 명시적 제외가 우선한다

    # ── PolicyPort 구현 ──────────────────────────────────────────────
    def allows_capability(self, capability: str) -> PolicyDecision:
        flag = CAPABILITY_FLAGS.get(capability)
        if flag is None:
            return PolicyDecision(
                allowed=False, rule="UNKNOWN_CAPABILITY",
                detail=f"{capability!r} 는 정책이 모르는 권한이다 — 모르면 거부한다",
            )
        if getattr(self, flag):
            return _OK
        return PolicyDecision(
            allowed=False, rule=f"{capability.upper()}_FORBIDDEN",
            detail=f"engagement 가 {capability} 를 허용하지 않았다",
        )

    def check_concurrency(self, concurrency: int) -> PolicyDecision:
        if concurrency <= self.max_concurrency:
            return _OK
        return PolicyDecision(
            allowed=False, rule="CONCURRENCY_OVER_LIMIT",
            detail=f"동시 {concurrency} > 정책 상한 {self.max_concurrency}",
        )

    def check_amount(self, amount: MoneyValue) -> PolicyDecision:
        if self.max_amount is None:
            return PolicyDecision(
                allowed=False, rule="AMOUNT_NOT_PERMITTED",
                detail="금액 상한이 승인되지 않았다 — 금액을 건드릴 수 없다",
            )
        if not amount.same_currency(self.max_amount):
            return PolicyDecision(
                allowed=False, rule="CURRENCY_NOT_PERMITTED",
                detail=f"{amount.currency} 는 승인 통화({self.max_amount.currency})가 아니다",
            )
        if abs(amount.amount) > self.max_amount.amount:
            return PolicyDecision(
                allowed=False, rule="AMOUNT_OVER_LIMIT",
                detail=f"{amount} > 정책 상한 {self.max_amount}",
            )
        return _OK

    def check_surface(self, method: str, path: str) -> PolicyDecision:
        for denied in self.denied_path_prefixes:
            if path.startswith(denied):
                return PolicyDecision(
                    allowed=False, rule="PATH_EXPLICITLY_DENIED",
                    detail=f"{method} {path} 는 제외 범위 {denied!r} 안이다",
                )
        if any(path.startswith(pre) for pre in self.allowed_path_prefixes):
            return _OK
        return PolicyDecision(
            allowed=False, rule="OUT_OF_SCOPE_PATH",
            detail=f"{method} {path} 는 승인 범위 {self.allowed_path_prefixes} 밖이다",
        )

    def check_budget(self, requests: int, duration_s: float) -> PolicyDecision:
        if requests > self.max_requests_per_step:
            return PolicyDecision(
                allowed=False, rule="REQUEST_BUDGET_EXCEEDED",
                detail=f"요청 {requests} > 스텝 상한 {self.max_requests_per_step}",
            )
        if duration_s > self.max_duration_s:
            return PolicyDecision(
                allowed=False, rule="DURATION_BUDGET_EXCEEDED",
                detail=f"{duration_s}s > 상한 {self.max_duration_s}s",
            )
        return _OK

    def policy_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ── 매니페스트 단위 검사 ─────────────────────────────────────────────────


def check_manifest(
    policy: PolicyPort,
    manifest: ToolManifest,
    params: dict | None = None,
) -> list[PolicyDecision]:
    out: list[PolicyDecision] = []

    for cap in manifest.capabilities.required:
        d = policy.allows_capability(cap)
        if not d.allowed:
            out.append(d)

    d = policy.check_concurrency(manifest.capabilities.concurrency)
    if not d.allowed:
        out.append(d)

    d = policy.check_budget(
        manifest.capabilities.request_budget, manifest.capabilities.max_duration_s
    )
    if not d.allowed:
        out.append(d)

    # 정적 금액 상한 — 매니페스트가 "이만큼까지 건드린다" 고 선언한 값
    if manifest.capabilities.max_amount_touched is not None:
        d = policy.check_amount(MoneyValue(
            amount=manifest.capabilities.max_amount_touched,
            currency=(getattr(policy, "max_amount", None).currency
                      if getattr(policy, "max_amount", None) else "USD"),
        ))
        if not d.allowed:
            out.append(d)

    # ★ 런타임 금액 — 정적 상한으로는 못 잡는다. policy_checked 파라미터를 다시 본다.
    for name in manifest.amount_params():
        if not params or params.get(name) is None:
            continue
        spec = manifest.params[name]
        try:
            value = MoneyValue(
                amount=params[name],
                currency=spec.currency or (
                    getattr(policy, "max_amount", None).currency
                    if getattr(policy, "max_amount", None) else "USD"),
            )
        except Exception as exc:
            out.append(PolicyDecision(
                allowed=False, rule="AMOUNT_UNPARSEABLE",
                detail=f"{name}: 금액으로 해석할 수 없어 정책 검사를 할 수 없다 ({exc})",
            ))
            continue
        d = policy.check_amount(value)
        if not d.allowed:
            out.append(d)

    # ★ 정적으로 선언된 표면도 승인 범위 안이어야 한다.
    #   접두어 선언(정찰 tool)은 울타리 자체가 범위 안인지 본다.
    for decl in manifest.surface:
        d = policy.check_surface(decl.method, decl.path)
        if not d.allowed:
            out.append(d)
    for pre in manifest.surface_prefixes:
        d = policy.check_surface(pre.methods[0] if pre.methods else "GET", pre.path_prefix)
        if not d.allowed:
            out.append(d)

    return out
