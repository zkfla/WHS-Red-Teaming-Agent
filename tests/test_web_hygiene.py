"""웹 위생 오라클 + 프로브 스파인 테스트 (CORS/verbose error/user enum).

오라클은 순수 boolean/문자열 신호로, 프로브는 MockTransport 로 결정적으로 검사한다.
"""
from __future__ import annotations

import httpx

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import (
    Engagement, EngagementActions, EngagementScope, EngagementTarget,
)
from finredteam.models.core import Verdict
from finredteam.oracles.generic import check_cors_policy, check_user_enumeration, check_verbose_error
from finredteam.probing.generic import hygiene
from finredteam.probing.session import Session
from finredteam.targets.loader import load_target


# ── 오라클 단위 ──────────────────────────────────────────────────────

def test_cors_reflection_is_violation():
    assert check_cors_policy("https://evil.example", "https://evil.example").verdict == Verdict.VIOLATION

def test_cors_wildcard_is_violation():
    assert check_cors_policy("*", "https://evil.example").verdict == Verdict.VIOLATION

def test_cors_restricted_or_absent_passes():
    assert check_cors_policy("https://bank.example", "https://evil.example").verdict == Verdict.PASS
    assert check_cors_policy(None, "https://evil.example").verdict == Verdict.PASS

def test_verbose_error_markers_violation_else_pass():
    assert check_verbose_error(["psycopg2"]).verdict == Verdict.VIOLATION
    assert check_verbose_error([]).verdict == Verdict.PASS

def test_user_enumeration_distinguishable_violation():
    assert check_user_enumeration(True, "존재=200, 비존재=404").verdict == Verdict.VIOLATION
    assert check_user_enumeration(False).verdict == Verdict.PASS


# ── 프로브 스파인 (MockTransport) ────────────────────────────────────

def _policy():
    eng = Engagement(
        engagement_id="wh-test",
        target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
        actions=EngagementActions(allow_state_change=True),
    )
    return compile_policy(eng)


def _session(handler):
    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_probe_cors_reflection_flows_to_violation():
    def handler(request):
        origin = request.headers.get("origin", "")
        return httpx.Response(200, json={"status": "success"}, headers={"Access-Control-Allow-Origin": origin})
    out = hygiene.probe_cors_reflection(_session(handler))
    assert out.verdict == Verdict.VIOLATION and out.invariant == "G-CORS"


def test_probe_verbose_error_flows_to_violation():
    def handler(request):
        return httpx.Response(500, json={"status": "error", "message": "psycopg2.errors.SyntaxError: unterminated quoted string"})
    out = hygiene.probe_verbose_error(_session(handler))
    assert out.verdict == Verdict.VIOLATION and out.invariant == "G-INFO"


def test_probe_user_enumeration_flows_to_violation():
    def handler(request):
        import json as _j
        body = _j.loads(request.content or b"{}")
        if body.get("username") == "alice":
            return httpx.Response(200, json={"status": "success", "message": "Reset PIN sent"})
        return httpx.Response(404, json={"status": "error", "message": "User not found"})
    out = hygiene.probe_user_enumeration(_session(handler), "alice")
    assert out.verdict == Verdict.VIOLATION and out.invariant == "G-USERENUM"


def test_probe_cors_no_header_passes():
    def handler(request):
        return httpx.Response(200, json={"status": "success"})  # ACAO 없음
    assert hygiene.probe_cors_reflection(_session(handler)).verdict == Verdict.PASS
