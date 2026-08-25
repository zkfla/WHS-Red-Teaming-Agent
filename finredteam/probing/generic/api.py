"""BOLA, BFLA, Mass Assignment, JWT — active probing. `oracles/generic.py` 로 판정한다."""
from __future__ import annotations

import base64
import json

from finredteam.oracles.base import OracleOutcome, unknown
from finredteam.oracles.generic import check_bola_signature, check_jwt_none_alg
from finredteam.probing.session import Session, SessionError


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge_none_alg_token(payload: dict) -> str:
    """서명 없는(alg=none) JWT 를 위조한다 — 서버가 이를 수락하는지 시험."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}."


def probe_jwt_none_alg(session: Session, protected_path: str, method: str, payload: dict) -> OracleOutcome:
    token = forge_none_alg_token(payload)
    try:
        ex = session.raw_token_request(method, protected_path, token)
    except SessionError:
        return unknown("G-JWT", "요청 실패")

    accepted = ex.status_code < 400
    return check_jwt_none_alg(accepted_unsigned=accepted, alg_header_mutable=True)


def probe_bola(
    session: Session, path: str, method: str, requester_principal, entitlement_known: bool | None
) -> OracleOutcome:
    """다른 주체의 식별자로 요청해서 200 이 반환되는지 본다. entitlement 확인은
    호출부(knowledge/graph.py 조회 결과)가 넘겨준다 — 이 함수는 네트워크만 담당한다.
    """
    try:
        ex = session.authed_request(method, path, requester_principal)
    except SessionError:
        return unknown("G-BOLA", "요청 실패")

    cross_principal_object_returned = ex.status_code < 400
    return check_bola_signature(cross_principal_object_returned, entitlement_known)
