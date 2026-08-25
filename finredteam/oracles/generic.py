"""Generic Layer 판정 (§15) — 결정적 금융 불변식이 없다. 정직하게 인정하고 다르게
다룬다: VIOLATION 은 재현 가능한 강한 신호일 때만, 그 외엔 ANOMALY 로 낮춘다.

ANOMALY 비율이 높은 게 이 레이어의 정상적인 한계다 — Finance Layer 가 왜 다른지
보여주는 대조군이다. 무리하게 VIOLATION 으로 승격하지 않는다.
"""
from __future__ import annotations

from finredteam.oracles.base import OracleOutcome, anomaly, passed, violation

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
