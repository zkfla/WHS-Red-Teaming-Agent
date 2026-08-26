"""probing/session.py — 특히 어댑터 `auth` 설정이 없을 때(§ Human pre-analysis
제거, `targets/loader.py::minimal_target_config`)의 동작을 검증한다.

★ 실측 버그 회귀 가드: `targets/configs/<name>.yaml` 자체가 없어도 실행이 되게
바꿨는데(`AdapterConfig.auth: AuthConfig | None`), `Session.register/login/
authed_request`가 여전히 `self.target.adapter.auth.xxx`를 직접 읽으면
`AttributeError`로 못생기게 죽는다 — 명확한 `SessionError`로 바뀌어야 한다.
그리고 Auth Discovery가 찾은 헤더 스킴이 세션에 반영되면, 정적 config가
없어도(또는 있어도) 그걸 우선 써야 한다.
"""
from __future__ import annotations

import httpx
import pytest

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import Engagement, EngagementActions, EngagementScope, EngagementTarget
from finredteam.models.core import Principal
from finredteam.probing.session import Session, SessionError
from finredteam.targets.loader import minimal_target_config


def _policy():
    engagement = Engagement(
        engagement_id="t",
        target=EngagementTarget(name="t", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
        actions=EngagementActions(allow_state_change=True),
    )
    return compile_policy(engagement)


def _minimal_session(transport: httpx.MockTransport | None = None) -> Session:
    target = minimal_target_config("t", "http://localhost:5000")
    return Session(target, _policy(), _transport=transport)


# ── auth=None 일 때 명확한 SessionError (AttributeError 아님) ──────────


def test_register_raises_clear_session_error_when_auth_missing():
    session = _minimal_session()
    with pytest.raises(SessionError, match="Auth Discovery"):
        session.register("p1", "user", "pw")


def test_login_raises_clear_session_error_when_auth_missing():
    session = _minimal_session()
    principal = Principal(id="p1", username="user", password="pw")
    with pytest.raises(SessionError, match="Auth Discovery"):
        session.login(principal)


def test_authed_request_raises_clear_error_when_no_scheme_available():
    session = _minimal_session()
    principal = Principal(id="p1", username="user", password="pw", token="sometoken")
    with pytest.raises(SessionError, match="헤더 스킴"):
        session.authed_request("GET", "/dashboard", principal)


# ── discovered header 가 우선 적용됨 ────────────────────────────────────


def test_authed_request_uses_discovered_header_scheme():
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    session = _minimal_session(httpx.MockTransport(handler))
    session.set_discovered_auth_header("Authorization", "Bearer {token}")
    principal = Principal(id="p1", username="user", password="pw", token="abc123")

    session.authed_request("GET", "/dashboard", principal)

    assert captured_headers.get("authorization") == "Bearer abc123"


def test_authed_request_discovered_header_takes_priority_when_both_present(monkeypatch):
    # 정적 config 도 있고 discovered 헤더도 있으면 discovered 가 이겨야 한다
    # (Auth Discovery 가 실제로 검증까지 한 값이 더 신뢰도가 높다).
    from finredteam.targets.loader import AdapterConfig, AuthConfig, AuthEndpoint, TargetConfig, TargetSettings
    from finredteam.money import Currency

    target = TargetConfig(
        adapter=AdapterConfig(
            name="t",
            base_url="http://localhost:5000",
            default_currency=Currency(code="USD", minor_unit=2),
            auth=AuthConfig(
                register=AuthEndpoint(method="POST", path="/register"),
                login=AuthEndpoint(method="POST", path="/login"),
                token_header="Authorization: StaticScheme {token}",
            ),
        ),
        settings=TargetSettings(principals=["p1"]),
    )

    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    session = Session(target, _policy(), _transport=httpx.MockTransport(handler))
    session.set_discovered_auth_header("Authorization", "Bearer {token}")
    principal = Principal(id="p1", username="user", password="pw", token="abc123")

    session.authed_request("GET", "/dashboard", principal)

    assert captured_headers.get("authorization") == "Bearer abc123"  # StaticScheme 아님


def test_authed_request_falls_back_to_static_config_when_no_discovery():
    from finredteam.targets.loader import AdapterConfig, AuthConfig, AuthEndpoint, TargetConfig, TargetSettings
    from finredteam.money import Currency

    target = TargetConfig(
        adapter=AdapterConfig(
            name="t",
            base_url="http://localhost:5000",
            default_currency=Currency(code="USD", minor_unit=2),
            auth=AuthConfig(
                register=AuthEndpoint(method="POST", path="/register"),
                login=AuthEndpoint(method="POST", path="/login"),
                token_header="Authorization: Bearer {token}",
            ),
        ),
        settings=TargetSettings(principals=["p1"]),
    )

    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    session = Session(target, _policy(), _transport=httpx.MockTransport(handler))
    principal = Principal(id="p1", username="user", password="pw", token="abc123")

    session.authed_request("GET", "/dashboard", principal)  # set_discovered_auth_header 안 부름

    assert captured_headers.get("authorization") == "Bearer abc123"
