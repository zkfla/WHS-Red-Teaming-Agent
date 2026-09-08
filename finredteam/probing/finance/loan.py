"""대출(loan) 금액 정책 시험 오케스트레이션 — INV-5(한도/정책) 재료 생성.

실제 소스(app.py::request_loan)는 "No input validation on amount" — 클라이언트가
보낸 amount 를 검증 없이 그대로 INSERT 한다. 그래서 음수·0·비정상적으로 큰 금액을
보내도 서버가 수락(200 success)한다(GT F-10 음수/0 대출, F-11 최대한도 없음).

★ 판정은 여기서 하지 않는다 — 이 프로브는 "시도 금액 + 수락 여부"를 관측해
`ExecutionResult.policy_signals` 에 담아 돌려줄 뿐이고, 판정은 호출부가
`oracles/limit.py::check_inv_5`(run_all 경유)로 낸다(§설계원칙 3: 오라클은 코드가,
프로브는 재료만).

request_loan 은 잔액을 크레딧하지 않고 loans 테이블에만 INSERT 하므로 관측 가능한
상태 델타가 없다 — INV-5 는 델타가 아니라 policy_signals(금액·수락)로 판정한다.
"""
from __future__ import annotations

import secrets

from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.probing.session import Session

_LOAN_PATH = "/request_loan"


def run_loan_amount_policy_probe(
    session: Session,
    owner: Principal,
    *,
    amount: str = "-500.00",
    currency: str = "USD",
    min_policy: str | None = None,
    max_policy: str | None = None,
) -> tuple[CandidateFinding, ExecutionResult]:
    """`amount` 로 대출을 요청하고, 수락 여부를 policy_signals 로 담아 돌려준다.

    min_policy/max_policy 는 프로브가 주장하는 정책 경계다(서버가 선언한 값이 아니라
    도메인 기준). 음수/0(양수성)은 경계 없이도 위반이라 대개 그냥 두면 되고, "최대한도
    없음"(F-11) 을 증명하려면 큰 금액과 함께 max_policy 를 주면 INV-5 가 초과를 잡는다.
    """
    ex = session.authed_request("POST", _LOAN_PATH, owner, json_body={"amount": amount})
    accepted = (
        200 <= ex.status_code < 300
        and isinstance(ex.response_body, dict)
        and ex.response_body.get("status") == "success"
    )

    policy_signals: dict[str, object] = {
        "amount": amount,
        "accepted": accepted,
        "operation_label": "loan",
        "currency": currency,
    }
    if min_policy is not None:
        policy_signals["min"] = min_policy
    if max_policy is not None:
        policy_signals["max"] = max_policy

    candidate = CandidateFinding(
        id=f"loan-{secrets.token_hex(3)}",
        layer="finance",
        operation="request_loan",  # ★ episodic/untried_probes 매핑 키와 일치
        principal_id=owner.id,
        method="POST",
        path=_LOAN_PATH,
        params={"amount": amount},
        rationale=f"대출 금액 정책 시험(amount={amount})",
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[ex.status_code],
        policy_signals=policy_signals,
        response_texts=[ex.response_text[:200]],
    )
    return candidate, result
