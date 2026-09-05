"""probing/generic/web.py::probe_sqli_boolean — SQLi active probe 배선 테스트.

오라클(oracles/generic.py::check_sqli_signature)의 순수 판정은 test_generic_oracle.py
가 검증한다. 여기서는 프로브가 실제 세션으로 3개 payload(참/거짓/에러)를 보내고
그 응답으로 오라클을 호출해 VIOLATION 까지 이어지는지(=run_probe sqli_scan 이 쓰는
경로)를 MockTransport 로 확인한다.
"""
from __future__ import annotations

from urllib.parse import unquote

import httpx

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import Engagement, EngagementScope, EngagementTarget
from finredteam.models.core import Verdict
from finredteam.probing.generic.api import forge_hs256_token, probe_jwt_weak_secret
from finredteam.probing.generic.web import probe_sqli_boolean
from finredteam.probing.session import Session
from finredteam.targets.loader import load_target


def _policy():
    eng = Engagement(
        engagement_id="t", target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
    )
    return compile_policy(eng)


def _session(handler) -> Session:
    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_sqli_probe_flags_violation_on_error_plus_boolean_diff():
    """참(OR '1'='1')과 거짓(OR '1'='2') 응답이 다르고, 에러 payload 가 SQL 에러
    시그니처를 노출하면 → G-SQLI VIOLATION."""
    def handler(request: httpx.Request) -> httpx.Response:
        p = unquote(str(request.url.path))
        if "'='1" in p:  # true payload → 더 많은 행
            return httpx.Response(200, json={"rows": ["A", "B", "C"]})
        if "'='2" in p:  # false payload → 적은 행 (boolean 차분)
            return httpx.Response(200, json={"rows": ["A"]})
        # 짝 안 맞는 따옴표 → DB 에러 노출
        return httpx.Response(200, text="psycopg2.errors.SyntaxError: unterminated quoted string")

    outcome = probe_sqli_boolean(_session(handler), "/check_balance/{value}", "1234567890")
    assert outcome.verdict == Verdict.VIOLATION
    assert outcome.invariant == "G-SQLI"


def test_sqli_probe_no_signal_passes():
    """참/거짓 응답이 동일하고 에러도 없으면 → 위반 아님(거짓양성 방지)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": ["A"]})  # 항상 같은 응답

    outcome = probe_sqli_boolean(_session(handler), "/check_balance/{value}", "1234567890")
    assert outcome.verdict != Verdict.VIOLATION


def test_jwt_weak_secret_forgery_flags_violation():
    """서버가 약한 시크릿('secret123')으로 서명된 토큰을 정상으로 받아들이면 → G-JWT VIOLATION.
    목 서버는 사전에 있는 secret123 으로 검증했을 때만 200 을 준다."""
    import base64, hashlib, hmac, json

    def _valid(token: str, secret: str) -> bool:
        try:
            h, b, sig = token.split(".")
            expected = base64.urlsafe_b64encode(
                hmac.new(secret.encode(), f"{h}.{b}".encode(), hashlib.sha256).digest()
            ).rstrip(b"=").decode()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        # ★ 취약점: 약한 하드코딩 시크릿으로 검증
        return httpx.Response(200 if _valid(token, "secret123") else 401, json={})

    outcome = probe_jwt_weak_secret(_session(handler), "/api/virtual-cards", "GET", {"user_id": 1, "is_admin": True})
    assert outcome.verdict == Verdict.VIOLATION
    assert outcome.invariant == "G-JWT"


def test_jwt_strong_secret_no_forgery():
    """서버가 사전에 없는 강한 시크릿을 쓰면 위조 실패 → 위반 아님(거짓양성 방지)."""
    import base64, hashlib, hmac

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        try:
            h, b, sig = token.split(".")
            expected = base64.urlsafe_b64encode(
                hmac.new(b"a-very-strong-unguessable-key-9f3x", f"{h}.{b}".encode(), hashlib.sha256).digest()
            ).rstrip(b"=").decode()
            ok = hmac.compare_digest(sig, expected)
        except Exception:
            ok = False
        return httpx.Response(200 if ok else 401, json={})

    outcome = probe_jwt_weak_secret(_session(handler), "/api/virtual-cards", "GET", {"user_id": 1})
    assert outcome.verdict != Verdict.VIOLATION
