"""Generic Layer 판정 (§15) — 결정적 금융 불변식이 없다. 정직하게 인정하고 다르게
다룬다: VIOLATION 은 재현 가능한 강한 신호일 때만, 그 외엔 ANOMALY 로 낮춘다.

ANOMALY 비율이 높은 게 이 레이어의 정상적인 한계다 — Finance Layer 가 왜 다른지
보여주는 대조군이다. 무리하게 VIOLATION 으로 승격하지 않는다.
"""
from __future__ import annotations

from finredteam.oracles.base import OracleOutcome, anomaly, passed, unknown, violation

_SQL_ERROR_MARKERS = (
    "sql syntax", "psycopg2", "sqlite3.", "postgresql", "you have an error in your sql",
    "unterminated quoted", "syntax error at or near", "pg::",
)


def check_sqli_signature(probe_body: str, boolean_diff: bool = False, time_delay: bool = False) -> OracleOutcome:
    lowered = (probe_body or "").lower()
    has_error = any(marker in lowered for marker in _SQL_ERROR_MARKERS)

    if has_error and (boolean_diff or time_delay):
        return violation("G-SQLI", "에러 시그니처 + boolean/time 차분 재현")
    if has_error:
        return anomaly("G-SQLI", "에러 메시지만 확인됨 (부작용/차분 재현 없음)")
    if boolean_diff or time_delay:
        return anomaly("G-SQLI", "boolean/time 차분 신호만 확인됨 (에러 시그니처 없음)")
    return passed("G-SQLI", "SQLi 신호 없음")


def check_reflected_xss(reflected: bool, escaped: bool, executable_context: bool) -> OracleOutcome:
    if not reflected:
        return passed("G-XSS", "입력이 응답에 반영되지 않음")
    if not escaped and executable_context:
        return violation("G-XSS", "이스케이프 없이 실행 가능한 컨텍스트에 반영됨")
    return anomaly("G-XSS", "문자열만 반영됨 (실행 가능한 컨텍스트/이스케이프 여부 미확인)")


def check_bola_signature(cross_principal_object_returned: bool, entitlement_known: bool | None) -> OracleOutcome:
    if not cross_principal_object_returned:
        return passed("G-BOLA", "타 주체 객체가 반환되지 않음")
    if entitlement_known is None:
        return anomaly("G-BOLA", "200 응답만 확인됨 — entitlement 확인 안 됨")
    if entitlement_known is False:
        return violation("G-BOLA", "타 주체 객체 반환 + entitlement 없음 확인")
    return passed("G-BOLA", "entitlement 보유 확인됨")


def check_jwt_none_alg(accepted_unsigned: bool, alg_header_mutable: bool = False) -> OracleOutcome:
    if accepted_unsigned:
        return violation("G-JWT", "서명 없는 토큰(alg=none)을 서버가 수락함")
    if alg_header_mutable:
        return anomaly("G-JWT", "alg 헤더 수정은 가능했으나 서버가 수락하지 않음")
    return passed("G-JWT", "alg 조작이 거부됨")


def check_jwt_forgery(accepted: bool, weak_secret_found: str | None = None) -> OracleOutcome:
    """약한/기본 시크릿으로 HS256 서명한 위조 토큰을 서버가 정상 토큰으로 수락하면
    위반 — 공격자가 임의 사용자로 토큰을 위조할 수 있다(§ GT G-03 Token Forgery).
    실제 시크릿 값은 판정 근거에 남기지 않는다(추가 유출 방지)."""
    if accepted:
        return violation(
            "G-JWT",
            "약한/기본 서명키로 위조한 HS256 토큰을 서버가 수락함 — 임의 사용자 토큰 위조 가능",
            evidence={"weak_secret_found": bool(weak_secret_found)},
        )
    return passed("G-JWT", "흔한 약한 시크릿으로는 토큰 위조가 수락되지 않음")


# ── 웹 위생(Web hygiene) 오라클 — 상태-효과가 아니라 헤더/에러/응답차이 신호로 판정.
#    Generic Layer 원칙(§15) 그대로: 재현 가능한 강한 신호만 VIOLATION, 애매하면 낮춘다.


def check_cors_policy(acao: str | None, origin_sent: str, allow_credentials: bool = False) -> OracleOutcome:
    """CORS 정책 — 임의 Origin 반사(reflection) 또는 전체 허용(*)을 위반으로 본다 (G-14)."""
    if not acao:
        return passed("G-CORS", "Access-Control-Allow-Origin 헤더 없음 — 교차출처 미허용")
    if acao == origin_sent:
        cred = " + credentials 허용(치명적)" if allow_credentials else ""
        return violation(
            "G-CORS", f"임의 Origin 반사: 보낸 Origin({origin_sent})을 그대로 허용{cred}",
            evidence={"acao": acao, "origin": origin_sent, "credentials": allow_credentials},
        )
    if acao == "*":
        return violation(
            "G-CORS", "모든 Origin 허용(Access-Control-Allow-Origin: *) — 금융 API에 부적절",
            evidence={"acao": "*"},
        )
    return passed("G-CORS", f"제한된 Origin만 허용(ACAO={acao})")


def check_verbose_error(markers_found: list[str]) -> OracleOutcome:
    """상세 예외 노출 — 에러 응답에 DB/프레임워크/스택 마커가 있으면 위반 (G-08)."""
    if markers_found:
        return violation(
            "G-INFO", f"에러 응답에 내부 구현 노출 마커: {', '.join(markers_found[:5])}",
            evidence={"markers": markers_found[:8]},
        )
    return passed("G-INFO", "에러 응답에서 내부 구현 노출 신호 없음")


def check_user_enumeration(distinguishable: bool, detail: str = "") -> OracleOutcome:
    """사용자 열거 — 존재/비존재 계정의 응답이 구별되면 위반 (G-18)."""
    if distinguishable:
        return violation("G-USERENUM", f"존재/비존재 계정 응답이 구별됨 — {detail}", evidence={"detail": detail})
    return passed("G-USERENUM", "존재/비존재 계정 응답이 구별되지 않음")


def check_jwt_expiration(decoded: bool, has_exp: bool) -> OracleOutcome:
    """JWT 만료 — 발급된 토큰에 exp(만료) 클레임이 없으면 위반 (G-07). 영구 유효 토큰."""
    if not decoded:
        return unknown("G-JWT-EXP", "토큰 페이로드 디코드 실패 — 만료 클레임 판정 불가")
    if not has_exp:
        return violation("G-JWT-EXP", "발급된 JWT에 exp(만료) 클레임 없음 — 토큰이 영구 유효")
    return passed("G-JWT-EXP", "JWT에 exp(만료) 클레임 존재")


def check_jwt_in_query(requires_auth: bool, query_accepted: bool) -> OracleOutcome:
    """JWT in URL Query — 보호 엔드포인트가 ?token= 쿼리의 토큰을 인증으로 수락하면 위반 (G-11).

    query 로 토큰을 넘기면 URL 로그·Referer·히스토리로 토큰이 유출된다. requires_auth 가
    거짓이면(엔드포인트가 애초에 인증 불요) query 토큰 판정 자체가 무의미 → UNKNOWN.
    """
    if not requires_auth:
        return unknown("G-JWT-QUERY", "대상 엔드포인트가 토큰 없이도 접근됨 — 쿼리 토큰 판정 불가")
    if query_accepted:
        return violation("G-JWT-QUERY", "보호 엔드포인트가 URL 쿼리(?token=)의 토큰을 인증 수단으로 수락")
    return passed("G-JWT-QUERY", "URL 쿼리 토큰이 인증으로 수락되지 않음")


# ── 인증 위생(Auth hygiene) 오라클 — 전부 범용 신호 기반(대상 문자열 없음) ──


def check_weak_password_policy(weak_accepted: bool, password: str = "") -> OracleOutcome:
    """약한 비밀번호 정책 — 지나치게 단순한 비번으로 가입이 수락되면 위반 (G-15)."""
    if weak_accepted:
        return violation("G-WEAKPW", f"약한 비밀번호({password!r})로 가입이 수락됨 — 비번 정책 부재")
    return passed("G-WEAKPW", "약한 비밀번호 가입이 거부됨")


def check_default_credentials(login_succeeded: bool, cred_label: str = "") -> OracleOutcome:
    """기본/약한 크레덴셜 — 흔한 기본 계정으로 로그인 성공하면 위반 (G-16)."""
    if login_succeeded:
        return violation("G-DEFAULTCRED", f"기본/약한 크레덴셜로 로그인 성공: {cred_label}")
    return passed("G-DEFAULTCRED", "시도한 기본 크레덴셜로 로그인 실패")


def check_rate_limit(attempts: int, throttled: bool) -> OracleOutcome:
    """인증 rate limit — N회 반복 인증 시도에도 throttle/lock 신호가 없으면 위반 (G-13)."""
    if attempts >= 2 and not throttled:
        return violation("G-RATELIMIT", f"{attempts}회 반복 인증 시도에도 throttle/lockout 없음 — rate limit 부재")
    if throttled:
        return passed("G-RATELIMIT", f"{attempts}회 내에 throttle/lockout 관측됨")
    return unknown("G-RATELIMIT", "시도 횟수 부족으로 판정 불가")


def check_csrf(statechange_via_cookie_accepted: bool, samesite_present: bool) -> OracleOutcome:
    """CSRF — 상태변경이 SameSite 없는 쿠키 인증으로 CSRF 토큰 없이 수락되면 위반 (G-12)."""
    if statechange_via_cookie_accepted and not samesite_present:
        return violation(
            "G-CSRF", "상태변경 요청이 SameSite 없는 쿠키 인증으로 CSRF 토큰 없이 수락됨 — CSRF 가능",
        )
    if samesite_present:
        return passed("G-CSRF", "인증 쿠키에 SameSite 속성 존재 — CSRF 기본 방어 있음")
    return passed("G-CSRF", "상태변경이 쿠키 인증만으로는 수락되지 않음")
