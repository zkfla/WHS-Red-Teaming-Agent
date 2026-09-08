"""청구서 결제(bill payment) 금액 정책 시험 — INV-5(한도/정책) 재료 생성.

핵심: billers 조회(app.py::get_billers_by_category)는 각 biller 의 서버-선언
`minimum_amount`/`maximum_amount` 를 응답한다. 그런데 결제 생성(create_bill_payment,
app.py:2066 "No amount validation")은 이 경계를 검사하지 않는다 — balance 방식은
잔액만 본다(`amount > balance`). 그래서 음수/0(F-21)이나 최소 미만(F-23) 금액을
보내도 수락된다. 프로브가 서버 선언 경계를 policy_signals 로 함께 담으므로, INV-5 는
프로브 주장이 아니라 **서버가 스스로 선언한 정책**을 기준으로 위반을 판정한다(강한 증거).

★ 판정은 오라클(oracles/limit.py::check_inv_5)이 한다 — 프로브는 재료만(§설계원칙 3).
"""
from __future__ import annotations

import secrets

from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.probing.session import Session

_CREATE_PATH = "/api/bill-payments/create"


def discover_biller(session: Session) -> dict | None:
    """공개 카테고리→biller 조회로 실존 biller 하나와 그 정책 경계를 찾는다.

    반환: {"id", "minimum_amount", "maximum_amount"} 또는 None(발견 실패).
    두 엔드포인트 모두 인증 불필요(소스상 @token_required 없음)라 anonymous 로 훑는다.
    """
    try:
        cats = session.anonymous_request("GET", "/api/bill-categories")
    except Exception:
        return None
    body = cats.response_body if isinstance(cats.response_body, dict) else {}
    for cat in body.get("categories", []) or []:
        cat_id = cat.get("id")
        if cat_id is None:
            continue
        try:
            bl = session.anonymous_request("GET", f"/api/billers/by-category/{cat_id}")
        except Exception:
            continue
        bbody = bl.response_body if isinstance(bl.response_body, dict) else {}
        billers = bbody.get("billers") or []
        if billers:
            b = billers[0]
            return {
                "id": b.get("id"),
                "minimum_amount": b.get("minimum_amount"),
                "maximum_amount": b.get("maximum_amount"),
            }
    return None


def run_bill_amount_policy_probe(
    session: Session,
    owner: Principal,
    *,
    biller: dict,
    amount: str = "-100.00",
    currency: str = "USD",
) -> tuple[CandidateFinding, ExecutionResult]:
    """`amount` 로 balance 방식 결제를 시도하고, biller 의 서버-선언 min/max 와 수락
    여부를 policy_signals 로 담는다."""
    ex = session.authed_request(
        "POST", _CREATE_PATH, owner,
        json_body={"biller_id": biller.get("id"), "amount": amount, "payment_method": "balance"},
    )
    body = ex.response_body if isinstance(ex.response_body, dict) else {}
    accepted = 200 <= ex.status_code < 300 and body.get("status") == "success"

    policy_signals: dict[str, object] = {
        "amount": amount,
        "accepted": accepted,
        "operation_label": "bill",
        "currency": currency,
    }
    mn = biller.get("minimum_amount")
    mx = biller.get("maximum_amount")
    if mn is not None:
        policy_signals["min"] = str(mn)
    if mx is not None:
        policy_signals["max"] = str(mx)

    candidate = CandidateFinding(
        id=f"bill-{secrets.token_hex(3)}",
        layer="finance",
        operation="bill_payment",
        principal_id=owner.id,
        method="POST",
        path=_CREATE_PATH,
        params={"biller_id": biller.get("id"), "amount": amount, "payment_method": "balance"},
        rationale=f"청구 결제 금액 정책 시험(amount={amount}, 서버선언 min={mn}/max={mx})",
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[ex.status_code],
        policy_signals=policy_signals,
        response_texts=[ex.response_text[:200]],
    )
    return candidate, result
