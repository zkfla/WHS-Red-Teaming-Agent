"""인증 위생(Auth hygiene) 프로브 — 약한 비번/기본 크레덴셜/rate limit/CSRF.

★ 범용 원칙: 판정은 범용 오라클(oracles/generic.py)이 신호로만 한다. 이 프로브는
대상 특유의 경로·필드·기본크레덴셜 목록을 **함수 인자의 기본값(config)**으로만 둔다 —
로직엔 안 박는다. 대상이 바뀌면 인자만 갈아끼우거나 recon/adapter 가 채우면 된다
(기본값은 vuln-bank 관례일 뿐).
"""
from __future__ import annotations

import secrets

from finredteam.models.core import Principal
from finredteam.oracles.base import OracleOutcome
from finredteam.oracles.generic import (
    check_csrf,
    check_default_credentials,
    check_rate_limit,
    check_weak_password_policy,
)
from finredteam.probing.session import Session

# 흔한 기본/약한 크레덴셜 목록 — 이 목록이 '범용화되는 config'다(대상별로 교체/확장).
_COMMON_CREDENTIALS = [
    ("admin", "admin"), ("admin", "admin123"), ("admin", "password"),
    ("admin", "123456"), ("root", "root"), ("administrator", "administrator"),
]


def probe_weak_password(
    session: Session, *, register_path: str = "/register",
    user_field: str = "username", pass_field: str = "password", weak_password: str = "123",
) -> OracleOutcome:
    """약한 비밀번호로 신규 가입을 시도하고 수락 여부를 관측."""
    username = f"weakpw_{secrets.token_hex(3)}"
    ex = session._request("POST", register_path, json_body={user_field: username, pass_field: weak_password})
    body = ex.response_body if isinstance(ex.response_body, dict) else {}
    accepted = 200 <= ex.status_code < 300 and body.get("status") != "error"
    return check_weak_password_policy(accepted, weak_password)


def probe_default_credentials(
    session: Session, *, login_path: str = "/login",
    user_field: str = "username", pass_field: str = "password", candidates=None,
) -> OracleOutcome:
    """흔한 기본 크레덴셜 목록으로 로그인해 하나라도 성공하는지 관측."""
    for user, pw in (candidates or _COMMON_CREDENTIALS):
        ex = session._request("POST", login_path, json_body={user_field: user, pass_field: pw})
        body = ex.response_body if isinstance(ex.response_body, dict) else {}
        if 200 <= ex.status_code < 300 and (body.get("token") or body.get("status") == "success"):
            return check_default_credentials(True, f"{user}/{pw}")
    return check_default_credentials(False, "")


def probe_rate_limit(
    session: Session, *, login_path: str = "/login",
    user_field: str = "username", pass_field: str = "password", attempts: int = 6,
) -> OracleOutcome:
    """같은 계정에 연속 로그인 실패를 N회 보내고 throttle/lockout(429/423) 신호를 관측."""
    username = f"rl_{secrets.token_hex(3)}"
    throttled = False
    for _ in range(attempts):
        ex = session._request("POST", login_path, json_body={user_field: username, pass_field: "wrong-pw-zzz"})
        if ex.status_code in (429, 423):
            throttled = True
            break
    return check_rate_limit(attempts, throttled)


def probe_csrf_statechange(
    session: Session, owner: Principal, *, login_path: str = "/login",
    statechange_path: str = "/transfer", user_field: str = "username", pass_field: str = "password",
    amount: str = "1.00",
) -> OracleOutcome:
    """로그인으로 인증 쿠키를 받고(SameSite 유무 관측), 그 쿠키만으로(Authorization 없이,
    CSRF 토큰 없이) 상태변경(자기계좌 소액 송금)이 수락되는지 관측한다."""
    lx = session._request("POST", login_path, json_body={user_field: owner.username, pass_field: owner.password})
    set_cookie = ""
    for k, v in lx.response_headers.items():
        if k.lower() == "set-cookie":
            set_cookie = v
    samesite_present = "samesite" in set_cookie.lower()
    body = lx.response_body if isinstance(lx.response_body, dict) else {}
    token = body.get("token")
    if not token or not owner.account_number:
        return check_csrf(False, samesite_present)  # 쿠키/계좌 확보 실패 → 상태변경 시험 불가
    # 쿠키만으로(헤더 Authorization 없이) 상태변경 — CSRF 토큰도 안 싣는다
    sx = session.request_with_header(
        "POST", statechange_path, "Cookie", f"token={token}",
        json_body={"amount": amount, "to_account": owner.account_number, "description": "csrf-probe"},
    )
    accepted = 200 <= sx.status_code < 300
    return check_csrf(accepted, samesite_present)
