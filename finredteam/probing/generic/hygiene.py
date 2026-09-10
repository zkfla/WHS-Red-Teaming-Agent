"""웹 위생(Web hygiene) 프로브 — CORS·상세에러·사용자열거.

상태-효과(돈 변화)가 아니라 헤더/에러메시지/응답차이라는 다른 종류의 신호를 관측해
Generic 오라클(oracles/generic.py)로 판정한다. 프로브는 재료(신호)를 만들고 판정은
오라클이 낸다(§설계원칙 3) — INV-* 프로브와 같은 분리다.
"""
from __future__ import annotations

from finredteam.oracles.base import OracleOutcome
from finredteam.oracles.generic import (
    check_cors_policy,
    check_jwt_expiration,
    check_jwt_in_query,
    check_user_enumeration,
    check_verbose_error,
)
from finredteam.probing.session import Session

# 에러 응답에서 '내부 구현 노출'로 볼 마커(소문자 비교). 정상 JSON 에 흔한 단어는
# 오탐이 되므로 DB/프레임워크/스택 고유 문구만 좁게 쓴다.
_ERROR_MARKERS = (
    "psycopg2", "sqlite3.", "syntax error at or near", "unterminated quoted",
    "you have an error in your sql", "pg::", "sqlalchemy",
    "traceback (most recent call last)", "werkzeug", 'file "', "line ",
)


def _header_ci(headers: dict[str, str], name: str) -> str | None:
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def probe_cors_reflection(
    session: Session, path: str = "/check_balance/1", origin: str = "https://evil.example"
) -> OracleOutcome:
    """임의 Origin 을 실어 보내고 Access-Control-Allow-Origin 반사/전체허용을 관측."""
    ex = session.request_with_header("GET", path, "Origin", origin)
    acao = _header_ci(ex.response_headers, "access-control-allow-origin")
    allow_cred = (_header_ci(ex.response_headers, "access-control-allow-credentials") or "").lower() == "true"
    return check_cors_policy(acao, origin, allow_cred)


def probe_verbose_error(session: Session, path: str = "/check_balance/'") -> OracleOutcome:
    """잘못된 입력으로 서버 예외를 유발하고 응답에 내부 구현 마커가 노출되는지 관측."""
    ex = session.anonymous_request("GET", path)
    text = (ex.response_text or "").lower()
    found = [m for m in _ERROR_MARKERS if m in text]
    return check_verbose_error(found)


def probe_user_enumeration(
    session: Session, existing_username: str, absent_username: str = "no_such_user_zzz_000"
) -> OracleOutcome:
    """존재/비존재 계정으로 forgot-password 를 쳐서 응답이 구별되는지 관측."""
    a = session.anonymous_request("POST", "/forgot-password", json_body={"username": existing_username})
    b = session.anonymous_request("POST", "/forgot-password", json_body={"username": absent_username})
    distinguishable = a.status_code != b.status_code
    detail = f"존재={a.status_code}, 비존재={b.status_code}"
    return check_user_enumeration(distinguishable, detail)


def _decode_jwt_payload(token: str) -> dict | None:
    """JWT 의 payload(가운데 세그먼트)를 서명 검증 없이 base64url 디코드한다.
    만료 클레임 유무만 보므로 검증은 불필요하고, PyJWT 의존도 피한다."""
    import base64
    import json

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)  # base64url 패딩 복원
        return json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return None


def probe_jwt_expiration(token: str) -> OracleOutcome:
    """발급된 토큰을 디코드해 exp(만료) 클레임 유무를 관측."""
    claims = _decode_jwt_payload(token)
    return check_jwt_expiration(decoded=claims is not None, has_exp=bool(claims and "exp" in claims))


def probe_jwt_in_query(session: Session, token: str, path: str = "/dashboard") -> OracleOutcome:
    """보호 엔드포인트를 (1) 토큰 없이 (2) ?token= 쿼리로 쳐서, 쿼리 토큰이 인증으로
    수락되는지 관측한다. (1)이 401/403 이어야 '원래 보호됨'이 확인된다(오탐 방지)."""
    no_token = session.anonymous_request("GET", path)
    with_query = session.anonymous_request("GET", f"{path}?token={token}")
    requires_auth = no_token.status_code in (401, 403)
    query_accepted = 200 <= with_query.status_code < 300
    return check_jwt_in_query(requires_auth, query_accepted)
