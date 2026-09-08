"""가상카드 한도(card limit) 정책 시험 — INV-5(한도/정책) 재료 생성.

실제 소스(app.py)의 두 지점이 카드 한도를 검증 없이 받는다:
  - create_virtual_card(1637): "No validation on card limit" — card_limit 을 그대로 INSERT.
  - update_card_limit(1812): mass assignment — 보낸 필드를 화이트리스트 없이 그대로 UPDATE.
둘 다 응답에 확정된 한도를 되돌려주므로(create=card_details.limit, update=
debug_info.card_details.card_limit), 음수/0 한도가 수락됐는지 자체 완결적으로 관측된다
(GT F-17 생성 한도, F-18 한도 수정).

★ 판정은 오라클(oracles/limit.py::check_inv_5)이 한다 — 이 프로브는 시도 한도값과
수락 여부를 policy_signals 에 담을 뿐(§설계원칙 3).

F-19(카드 limit 미적용/초과 결제 성공)는 어느 엔드포인트가 한도를 강제해야 하는지
소스상 모호해(결제는 balance 만 검사, 전용 /pay 없음) 이 프로브 범위에서 제외한다.
"""
from __future__ import annotations

import secrets

from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.probing.session import Session

_CREATE_PATH = "/api/virtual-cards/create"


def _signals(amount: str, accepted: bool, currency: str, label: str) -> dict[str, object]:
    return {"amount": amount, "accepted": accepted, "operation_label": label, "currency": currency}


def run_card_creation_limit_probe(
    session: Session,
    owner: Principal,
    *,
    card_limit: str = "-500.00",
    currency: str = "USD",
) -> tuple[CandidateFinding, ExecutionResult]:
    """`card_limit` 으로 카드 생성을 시도하고, 확정된 한도가 응답에 담겨 수락됐는지 관측."""
    ex = session.authed_request(
        "POST", _CREATE_PATH, owner, json_body={"card_limit": card_limit, "currency": currency}
    )
    body = ex.response_body if isinstance(ex.response_body, dict) else {}
    accepted = (
        200 <= ex.status_code < 300
        and body.get("status") == "success"
        and isinstance(body.get("card_details"), dict)
        and body["card_details"].get("limit") is not None
    )
    candidate = CandidateFinding(
        id=f"cardlim-c-{secrets.token_hex(3)}",
        layer="finance",
        operation="card_limit_create",
        principal_id=owner.id,
        method="POST",
        path=_CREATE_PATH,
        params={"card_limit": card_limit},
        rationale=f"카드 생성 한도 정책 시험(card_limit={card_limit})",
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[ex.status_code],
        policy_signals=_signals(card_limit, accepted, currency, "card_limit_create"),
        response_texts=[ex.response_text[:200]],
    )
    return candidate, result


def run_card_update_limit_probe(
    session: Session,
    owner: Principal,
    card_id: str,
    *,
    card_limit: str = "-1.00",
    currency: str = "USD",
) -> tuple[CandidateFinding, ExecutionResult]:
    """기존 카드의 한도를 `card_limit` 로 변경 시도하고, 응답이 그 한도로 확정됐는지 관측."""
    path = f"/api/virtual-cards/{card_id}/update-limit"
    ex = session.authed_request("POST", path, owner, json_body={"card_limit": card_limit})
    body = ex.response_body if isinstance(ex.response_body, dict) else {}
    details = (body.get("debug_info") or {}).get("card_details") if isinstance(body.get("debug_info"), dict) else None
    accepted = (
        200 <= ex.status_code < 300
        and body.get("status") == "success"
        and isinstance(details, dict)
        and details.get("card_limit") is not None
    )
    candidate = CandidateFinding(
        id=f"cardlim-u-{secrets.token_hex(3)}",
        layer="finance",
        operation="card_limit_update",
        principal_id=owner.id,
        method="POST",
        path=path,
        params={"card_limit": card_limit},
        rationale=f"카드 한도 수정 정책 시험(card_limit={card_limit})",
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[ex.status_code],
        policy_signals=_signals(card_limit, accepted, currency, "card_limit_update"),
        response_texts=[ex.response_text[:200]],
    )
    return candidate, result
