"""INV-5 — 한도/정책 검증. 금액이 정책 경계(양수·최소·최대·기기 한도)를 벗어났는데도
서버가 수락하면 위반.

★ 스펙(instruction.md §19)은 INV-5 를 "스텁만"으로 미뤘으나, 프로젝트가 금융 레드팀
커버리지 확장으로 방향을 틀면서(README 우선) loan/bill/card 한도 계열(GT F-10/F-11/
F-17/F-18/F-19/F-21/F-23)을 잡기 위해 실제 구현으로 되살렸다.

★ 순수성(§설계원칙 4, §8 규칙 6): 네트워크·LLM 없음. 프로브(추후 배선)가 관측한
파생 수치(result.policy_signals)만 본다 — INV-9/BFLA 가 boolean 신호를 보는 것과 같은
패턴이되, 한도 판정은 수치 비교라 금액/경계를 Decimal-safe 문자열로 담는다. 신호가
없으면 UNKNOWN(프로브가 아직 안 채웠을 뿐, 정상이라는 뜻이 아니다).

★ INV-6 과의 관계: INV-6 은 '거래 금액 정합성(부호·정밀도·서버 환산)'을 baseline
기준으로 본다. INV-5 는 그와 독립적으로 'loan/bill/card 의 정책 경계(양수·최소·최대·
기기 한도)'를 본다. 둘 다 매 실행에 돌지만 각자 자기 재료로만 판정하므로 겹쳐도 무방
(오히려 한 실험이 둘을 동시에 발화하는 게 run_all 의 의도).

policy_signals 계약 (프로브가 채운다):
  amount          : str      시도한 금액 (Decimal 안전 문자열, 필수)
  accepted        : bool     서버가 수락(2xx)했는가 (필수)
  currency        : str      통화 코드 (사유 문구용, 선택)
  min             : str|None 정책 최소 — 이 미만이 수락되면 위반, 없으면 null
  max             : str|None 정책 최대 — 이 초과가 수락되면 위반, 없으면 null
  enforced_limit  : str|None 기기/계정 한도 — 사용액이 이걸 넘으면 위반, 없으면 null
  usage_after     : str|None 실행 후 사용액/잔액 (enforced_limit 판정용), 없으면 null
  operation_label : str      loan/bill/card_limit 등 (사유 문구용, 선택)
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.base import OracleOutcome, passed, unknown, violation


def _dec(value: Any) -> Decimal | None:
    """문자열/수치를 Decimal 로. float 경유 없이(§4) str() 을 거친다. 실패 시 None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def check_inv_5(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    sig = result.policy_signals
    if not sig:
        return unknown("INV-5", "policy_signals 없음 — 한도/정책 판정 재료 미확보(프로브 미배선)")

    amount = _dec(sig.get("amount"))
    if amount is None:
        return unknown("INV-5", "amount 미관측 — 한도 판정 불가")

    if not bool(sig.get("accepted")):
        # 정책 경계를 벗어난(혹은 정상) 요청이 거부됐다 = 정책이 지켜짐.
        return passed("INV-5", f"요청이 거부됨(accepted=false, amount={amount}) — 정책 준수")

    label = sig.get("operation_label") or "operation"
    cur = sig.get("currency") or ""

    # 1) 양수성 — 0 이하 금액이 수락됨 (F-10 음수/0 대출, F-17 음수/0 카드한도, F-21 음수/0 결제)
    if amount <= 0:
        return violation(
            "INV-5",
            f"{label}: 0 이하 금액({amount} {cur})이 수락됨 — 양수성 위반",
            evidence={"amount": str(amount)},
        )

    # 2) 최소 정책 미만 (F-23 최소금액 검증 누락)
    mn = _dec(sig.get("min"))
    if mn is not None and amount < mn:
        return violation(
            "INV-5",
            f"{label}: 최소 정책({mn} {cur}) 미만 금액({amount} {cur})이 수락됨",
            evidence={"amount": str(amount), "min": str(mn)},
        )

    # 3) 최대 정책 초과 (F-11 대출 최대한도 없음, F-18 카드한도 정책범위 초과)
    mx = _dec(sig.get("max"))
    if mx is not None and amount > mx:
        return violation(
            "INV-5",
            f"{label}: 최대 정책({mx} {cur}) 초과 금액({amount} {cur})이 수락됨",
            evidence={"amount": str(amount), "max": str(mx)},
        )

    # 4) 기기/계정 한도 미적용 (F-19 카드 limit 초과 사용 허용)
    lim = _dec(sig.get("enforced_limit"))
    use = _dec(sig.get("usage_after"))
    if lim is not None and use is not None and use > lim:
        return violation(
            "INV-5",
            f"{label}: 한도({lim} {cur})를 초과한 사용액({use} {cur})이 허용됨 — 한도 미적용",
            evidence={"usage_after": str(use), "enforced_limit": str(lim)},
        )

    return passed("INV-5", f"{label}: 관측된 정책 경계 내(amount={amount} {cur})")
