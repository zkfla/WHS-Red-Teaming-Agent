"""인증 흐름(회원가입/비밀번호 재설정) 민감정보 노출(INV-9) 프로브.

실제 소스:
- app.py::register 는 응답 debug_data.raw_data 에 요청에 보낸 비밀번호를 그대로
  echo 한다(§ GT 유사: Registration Response Sensitive Data Exposure).
- app.py::/api/v1/forgot-password(v1) 는 응답 debug_info.pin 에 재설정 PIN 을
  그대로 노출한다 → 이 PIN 만으로 비밀번호 재설정·계정 탈취가 가능(§ GT G-06).

★ 순수성(§10): 프로브가 원문을 보고 boolean 만 뽑는다. 비밀번호는 "우리가 보낸
값이 응답에 그대로 있는가"로, PIN 은 "pin 필드가 값과 함께 있는가"로 판단하며,
값 자체는 ExecutionResult 에 담지 않는다. 판정은 oracles/exposure.py 가 boolean
으로만 한다.
"""
from __future__ import annotations

import json

from finredteam.models.finding import CandidateFinding, ExecutionResult


def _contains_value(body: object, needle: str) -> bool:
    """응답 어딘가에 정확히 `needle`(우리가 보낸 비밀번호) 문자열이 있으면 True."""
    try:
        return needle in json.dumps(body, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return False


def _has_pin_field(body: object) -> bool:
    if isinstance(body, dict):
        for key, value in body.items():
            if str(key).lower() == "pin" and value not in (None, ""):
                return True
            if _has_pin_field(value):
                return True
    elif isinstance(body, list):
        return any(_has_pin_field(item) for item in body)
    return False


def run_register_password_echo_probe(
    session, username: str, password: str
) -> tuple[CandidateFinding, ExecutionResult]:
    """회원가입을 한 번 하고, 응답에 우리가 보낸 비밀번호가 그대로 들어있는지 본다."""
    ex = session._request("POST", "/register", json_body={"username": username, "password": password})
    echoed = _contains_value(ex.response_body, password)

    candidate = CandidateFinding(
        id=f"pwecho-{username}", layer="generic", operation="register_exposure",
        principal_id="anon", method="POST", path="/register",
    )
    result = ExecutionResult(
        candidate=candidate, status_codes=[ex.status_code],
        exposure_signals={"password_echoed": echoed},
    )
    return candidate, result


def run_forgot_password_pin_probe(
    session, username: str, *, path: str = "/api/v1/forgot-password"
) -> tuple[CandidateFinding, ExecutionResult]:
    """비밀번호 재설정을 요청하고, 응답에 재설정 PIN 이 노출되는지 본다."""
    ex = session._request("POST", path, json_body={"username": username})
    pin_exposed = _has_pin_field(ex.response_body)

    candidate = CandidateFinding(
        id=f"pinleak-{username}", layer="generic", operation="forgot_password_exposure",
        principal_id="anon", method="POST", path=path,
    )
    result = ExecutionResult(
        candidate=candidate, status_codes=[ex.status_code],
        exposure_signals={"reset_pin_exposed": pin_exposed},
    )
    return candidate, result
