"""SQLi, XSS, Path Traversal — active probing. `oracles/generic.py` 로 판정한다."""
from __future__ import annotations

from urllib.parse import quote

from finredteam.oracles.base import OracleOutcome, unknown
from finredteam.oracles.generic import check_sqli_signature
from finredteam.probing.session import Session, SessionError


def probe_sqli_boolean(session: Session, path_template: str, legit_value: str) -> OracleOutcome:
    """`path_template` 의 `{value}` 를 boolean-based payload 로 치환해 시도한다.

    passive 하지 않다 — 실제 injection payload 를 보내는 active probe 다.
    참(`OR '1'='1'`)과 거짓(`OR '1'='2'`) 응답의 차이를 본다.
    """
    true_payload = quote(f"{legit_value}' OR '1'='1", safe="")
    false_payload = quote(f"{legit_value}' OR '1'='2", safe="")
    error_payload = quote(f"{legit_value}'", safe="")  # 짝이 안 맞는 따옴표 — 에러 시그니처 유도용

    try:
        true_ex = session.anonymous_request("GET", path_template.replace("{value}", true_payload))
        false_ex = session.anonymous_request("GET", path_template.replace("{value}", false_payload))
        error_ex = session.anonymous_request("GET", path_template.replace("{value}", error_payload))
    except SessionError:
        return unknown("G-SQLI", "요청 실패")

    # 상태코드가 아니라 "둘 다 성공했지만 내용이 다르다"를 boolean-based 신호로 본다 —
    # 이 대상처럼 참/거짓 분기가 상태코드가 아니라 반환되는 레코드 자체로 갈리는
    # 경우(예: OR '1'='1' 이 무관한 다른 행을 끌고 오는 경우)를 잡기 위해서다.
    both_succeeded = true_ex.status_code < 400 and false_ex.status_code < 400
    boolean_diff = both_succeeded and (true_ex.response_body != false_ex.response_body)
    combined_body = "\n".join(
        ex.response_text or "" for ex in (true_ex, false_ex, error_ex)
    )
    return check_sqli_signature(combined_body, boolean_diff=boolean_diff)
