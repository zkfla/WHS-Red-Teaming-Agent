"""웹 위생(Web hygiene) 프로브 — CORS·상세에러·사용자열거.

상태-효과(돈 변화)가 아니라 헤더/에러메시지/응답차이라는 다른 종류의 신호를 관측해
Generic 오라클(oracles/generic.py)로 판정한다. 프로브는 재료(신호)를 만들고 판정은
오라클이 낸다(§설계원칙 3) — INV-* 프로브와 같은 분리다.
"""
from __future__ import annotations

from finredteam.oracles.base import OracleOutcome
from finredteam.oracles.generic import (
    check_cors_policy,
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
