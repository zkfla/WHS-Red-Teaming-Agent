"""카드정보 과다 노출(INV-9) 시험 프로브 (§ GT F-16).

실제 소스(app.py::get_virtual_cards)는 응답에 전체 card_number(PAN)와 CVV 를
마스킹 없이 그대로 담는다. 이 프로브는 응답 원문을 보고 "마스킹 없는 전체
PAN 이 있었나 / CVV 가 있었나"라는 boolean 만 뽑아 ExecutionResult.exposure_signals
에 담는다 — 값 자체는 담지 않는다(§10, §설계원칙 6). 판정은
oracles/exposure.py::check_inv_9 가 그 boolean 으로만 한다.
"""
from __future__ import annotations

import re

from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult

_PAN_RE = re.compile(r"^\d{13,19}$")  # 마스킹 없는 전체 PAN (숫자만 13~19자리)
_CVV_RE = re.compile(r"^\d{3,4}$")


def _scan(body: object) -> tuple[bool, bool]:
    """응답을 재귀 순회하며 (full_pan_exposed, cvv_exposed) 를 만든다.
    ★ 값은 밖으로 반환하지 않는다 — 오직 boolean 두 개만."""
    pan = cvv = False
    if isinstance(body, dict):
        for key, value in body.items():
            lk = str(key).lower()
            if isinstance(value, (str, int)) and _PAN_RE.match(str(value)) and (
                "card_number" in lk or "pan" in lk or lk == "number"
            ):
                pan = True
            if isinstance(value, (str, int)) and _CVV_RE.match(str(value)) and "cvv" in lk:
                cvv = True
            sub_pan, sub_cvv = _scan(value)
            pan = pan or sub_pan
            cvv = cvv or sub_cvv
    elif isinstance(body, list):
        for item in body:
            sub_pan, sub_cvv = _scan(item)
            pan = pan or sub_pan
            cvv = cvv or sub_cvv
    return pan, cvv


def run_card_exposure_probe(
    session, principal: Principal, *, path: str = "/api/virtual-cards"
) -> tuple[CandidateFinding, ExecutionResult]:
    """`path` 를 인증 요청으로 조회하고, 응답에 마스킹 없는 PAN/CVV 가 있는지
    boolean 신호만 뽑아 낸다. 판정은 호출부가 verify() 로 별도 실행한다."""
    ex = session.authed_request("GET", path, principal)
    pan_exposed, cvv_exposed = _scan(ex.response_body)

    candidate = CandidateFinding(
        id=f"cardexpo-{principal.id}",
        layer="finance",
        operation="card_exposure",
        principal_id=principal.id,
        method="GET",
        path=path,
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[ex.status_code],
        exposure_signals={"full_pan_exposed": pan_exposed, "cvv_exposed": cvv_exposed},
    )
    return candidate, result
