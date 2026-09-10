"""인증 위생 오라클 + 프로브 스파인 (G-15/16/13/12). 오라클은 범용 신호, 프로브는 MockTransport."""
from __future__ import annotations

import json

import httpx

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import Engagement, EngagementActions, EngagementScope, EngagementTarget
from finredteam.models.core import Principal, Verdict
from finredteam.oracles.generic import (
    check_csrf, check_default_credentials, check_rate_limit, check_weak_password_policy,
)
from finredteam.probing.generic import auth_hygiene
from finredteam.probing.session import Session
from finredteam.targets.loader import load_target

OWNER = Principal(id="a", username="alice", password="pw", account_number="111", token="t")


def _policy():
    eng = Engagement(engagement_id="ah", target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
                     scope=EngagementScope(allowed_hosts=["localhost"]), actions=EngagementActions(allow_state_change=True))
    return compile_policy(eng)

def _session(handler):
    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


# ── 오라클 단위 (범용) ──
def test_oracles():
    assert check_weak_password_policy(True, "123").verdict == Verdict.VIOLATION
    assert check_weak_password_policy(False).verdict == Verdict.PASS
    assert check_default_credentials(True, "admin/admin").verdict == Verdict.VIOLATION
    assert check_default_credentials(False).verdict == Verdict.PASS
    assert check_rate_limit(6, False).verdict == Verdict.VIOLATION
    assert check_rate_limit(6, True).verdict == Verdict.PASS
    assert check_csrf(True, False).verdict == Verdict.VIOLATION      # 쿠키 수락 + SameSite 없음
    assert check_csrf(True, True).verdict == Verdict.PASS            # SameSite 있음
    assert check_csrf(False, False).verdict == Verdict.PASS          # 쿠키로는 상태변경 안 됨


# ── 프로브 스파인 ──
def test_probe_weak_password_violation():
    def h(req): return httpx.Response(200, json={"status": "success"})
    assert auth_hygiene.probe_weak_password(_session(h)).verdict == Verdict.VIOLATION

def test_probe_default_credentials_violation():
    def h(req):
        b = json.loads(req.content or b"{}")
        if (b.get("username"), b.get("password")) == ("admin", "admin123"):
            return httpx.Response(200, json={"status": "success", "token": "tok"})
        return httpx.Response(401, json={"status": "error"})
    out = auth_hygiene.probe_default_credentials(_session(h))
    assert out.verdict == Verdict.VIOLATION and "admin/admin123" in out.reason

def test_probe_rate_limit_violation_when_never_throttled():
    def h(req): return httpx.Response(401, json={"status": "error"})  # 429 안 줌
    assert auth_hygiene.probe_rate_limit(_session(h), attempts=5).verdict == Verdict.VIOLATION

def test_probe_rate_limit_pass_when_throttled():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] >= 3 else 401, json={})
    assert auth_hygiene.probe_rate_limit(_session(h), attempts=6).verdict == Verdict.PASS

def test_probe_csrf_violation_no_samesite():
    def h(req):
        if req.url.path == "/login":
            return httpx.Response(200, json={"status": "success", "token": "tok"},
                                  headers={"Set-Cookie": "token=tok; HttpOnly"})  # SameSite 없음
        if req.url.path == "/transfer":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404, json={})
    out = auth_hygiene.probe_csrf_statechange(_session(h), OWNER)
    assert out.verdict == Verdict.VIOLATION and out.invariant == "G-CSRF"

def test_probe_csrf_pass_with_samesite():
    def h(req):
        if req.url.path == "/login":
            return httpx.Response(200, json={"status": "success", "token": "tok"},
                                  headers={"Set-Cookie": "token=tok; HttpOnly; SameSite=Strict"})
        return httpx.Response(200, json={"status": "success"})
    assert auth_hygiene.probe_csrf_statechange(_session(h), OWNER).verdict == Verdict.PASS
