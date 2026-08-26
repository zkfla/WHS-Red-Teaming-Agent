"""Engagement 가 제공한 테스트 계정(credentials)으로 부트스트랩하기 (engagement §3).

`target.settings.principals` 순서대로 매칭한다 — 있으면 등록 없이 로그인만,
부족한 만큼은 `allow_account_creation` 일 때만 새로 등록하고, 아니면 그 자리에서
실패한다(LLM에게 절대 안 맡기는 계정생성 판단 — engagement §14).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finredteam.agent.react import bootstrap_principals
from finredteam.engagement.errors import PolicyViolation
from finredteam.engagement.schema import EngagementCredential
from finredteam.models.core import Principal


class _StubExchange:
    def __init__(self, status_code: int, response_body: object = None):
        self.status_code = status_code
        self.response_body = response_body


class _StubSession:
    def __init__(self, discovery_responses: dict | None = None) -> None:
        self.login_calls: list[Principal] = []
        self.bootstrap_calls: list[tuple[str, str, str]] = []
        self.anon_calls: list[tuple[str, str]] = []
        self._discovery_responses = discovery_responses or {}
        self.discovered_auth_header: tuple[str, str] | None = None

    def login(self, principal: Principal) -> Principal:
        self.login_calls.append(principal)
        return principal.model_copy(update={"account_number": f"acc-{principal.username}", "token": "tok"})

    def bootstrap(self, principal_id: str, username: str, password: str) -> Principal:
        self.bootstrap_calls.append((principal_id, username, password))
        return Principal(id=principal_id, username=username, password=password, account_number=f"acc-{username}", token="tok")

    def anonymous_request(self, method, path, json_body=None):
        self.anon_calls.append((method, path))
        key = (method, path)
        if key not in self._discovery_responses:
            from finredteam.probing.session import SessionError

            raise SessionError(f"unexpected call: {key}")
        return self._discovery_responses[key]

    def request_with_header(self, method, path, header_name, header_value):
        key = (method, path)
        if key not in self._discovery_responses:
            from finredteam.probing.session import SessionError

            raise SessionError(f"unexpected call: {key}")
        return self._discovery_responses[key]

    def set_discovered_auth_header(self, header_name, value_template):
        self.discovered_auth_header = (header_name, value_template)


def _target(principal_ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(principals=principal_ids))


def test_provided_credentials_are_logged_in_not_registered():
    session = _StubSession()
    credentials = [EngagementCredential(username="alice", password="pw1")]

    principals, legacy_used = bootstrap_principals(
        session, _target(["attacker_A"]), credentials, allow_account_creation=False
    )

    assert len(session.login_calls) == 1
    assert session.login_calls[0].username == "alice"
    assert not session.bootstrap_calls
    assert principals[0].extra["bootstrap_source"] == "provisioned"
    assert legacy_used == ["login"]  # discovery_pages 를 안 줬으니 legacy 경로를 씀


def test_falls_back_to_self_registration_when_creation_allowed():
    session = _StubSession()
    # 3 principal 중 1개만 credential 제공 — 나머지 2개는 등록으로 채워야 함.
    credentials = [EngagementCredential(username="alice", password="pw1")]

    principals, _legacy_used = bootstrap_principals(
        session, _target(["attacker_A", "victim_V", "merchant_M"]), credentials, allow_account_creation=True
    )

    assert len(session.login_calls) == 1
    assert len(session.bootstrap_calls) == 2
    assert [p.id for p in principals] == ["attacker_A", "victim_V", "merchant_M"]
    assert principals[0].extra["bootstrap_source"] == "provisioned"
    assert principals[1].extra["bootstrap_source"] == "self_created"
    assert principals[2].extra["bootstrap_source"] == "self_created"


def test_fails_closed_when_credentials_insufficient_and_creation_forbidden():
    session = _StubSession()
    credentials = [EngagementCredential(username="alice", password="pw1")]

    with pytest.raises(PolicyViolation) as exc_info:
        bootstrap_principals(
            session, _target(["attacker_A", "victim_V"]), credentials, allow_account_creation=False
        )

    assert exc_info.value.rule == "ACCOUNT_CREATION_FORBIDDEN"
    assert "victim_V" in exc_info.value.detail
    assert not session.bootstrap_calls  # 부족한 쪽은 시도조차 안 해야 함


def test_no_credentials_behaves_like_before_when_creation_allowed():
    session = _StubSession()

    principals, legacy_used = bootstrap_principals(
        session, _target(["attacker_A", "victim_V"]), None, allow_account_creation=True
    )

    assert len(session.bootstrap_calls) == 2
    assert not session.login_calls
    assert all(p.extra["bootstrap_source"] == "self_created" for p in principals)
    assert legacy_used == ["login", "register"]


def test_no_credentials_and_creation_forbidden_fails_closed_for_all():
    session = _StubSession()

    with pytest.raises(PolicyViolation) as exc_info:
        bootstrap_principals(session, _target(["attacker_A"]), None, allow_account_creation=False)

    assert exc_info.value.rule == "ACCOUNT_CREATION_FORBIDDEN"


# ── Black-box Auth Discovery 우선, 실패 시 legacy 폴백(§11) ─────────────

_LOGIN_PAGE = """
<form id="loginForm">
    <input type="text" name="username">
    <input type="password" name="password">
</form>
<script>
document.getElementById('loginForm').addEventListener('submit', async (e) => {
    const r = await fetch('/login', {method: 'POST', headers: {}, body: JSON.stringify({})});
});
</script>
"""


def test_bootstrap_via_discovery_never_touches_target_dot_adapter():
    """§13 "No Pre-known Endpoint Test" — `_target()`가 반환하는 스텁은
    `.adapter`(로그인/회원가입 endpoint·필드명이 있는 LEGACY_BOOTSTRAP 어댑터
    설정) 자체가 아예 없다. discovery 로만 성공하면 이 속성에 접근할 일이
    없어야 한다 — 접근했다면 AttributeError 로 이 테스트 자체가 실패한다."""
    session = _StubSession(
        {
            ("POST", "/login"): _StubExchange(200, {"token": "tok-discovered", "accountNumber": "999"}),
            ("GET", "/dashboard"): _StubExchange(200),
        }
    )
    target = _target(["attacker_A"])
    assert not hasattr(target, "adapter")  # 전제 확인 — 어댑터 자체가 없는 대상

    principals, legacy_used = bootstrap_principals(
        session,
        target,
        [EngagementCredential(username="alice", password="pw1")],
        allow_account_creation=False,
        discovery_pages={"/login": _LOGIN_PAGE},
        protected_path="/dashboard",
    )

    assert legacy_used == []
    assert principals[0].token == "tok-discovered"


def test_bootstrap_prefers_discovery_over_legacy_when_it_succeeds():
    session = _StubSession(
        {
            ("POST", "/login"): _StubExchange(200, {"token": "tok-discovered", "accountNumber": "999"}),
            ("GET", "/dashboard"): _StubExchange(200),
        }
    )
    credentials = [EngagementCredential(username="alice", password="pw1")]

    principals, legacy_used = bootstrap_principals(
        session,
        _target(["attacker_A"]),
        credentials,
        allow_account_creation=False,
        discovery_pages={"/login": _LOGIN_PAGE},
        protected_path="/dashboard",
    )

    assert legacy_used == []  # discovery 로만 성공 — legacy 폴백 없음
    assert not session.login_calls  # 정적 config 경로(session.login)는 안 불림
    assert principals[0].token == "tok-discovered"
    # 실측 버그 회귀 가드: 발견된 헤더 스킴이 세션에 반영되지 않으면, 이후 모든
    # authed_request() 가 (존재하지도 않을 수 있는) 정적 adapter.auth.token_header
    # 로 조용히 폴백하다 크래시하거나 틀린 헤더로 나간다.
    assert session.discovered_auth_header == ("Authorization", "Bearer {token}")
    assert principals[0].account_number == "999"
    assert principals[0].extra["bootstrap_source"] == "provisioned"


def test_bootstrap_falls_back_to_legacy_when_discovery_finds_nothing():
    session = _StubSession({})  # discover_login 이 쓸 응답이 하나도 없음 -> 실패
    credentials = [EngagementCredential(username="alice", password="pw1")]

    principals, legacy_used = bootstrap_principals(
        session,
        _target(["attacker_A"]),
        credentials,
        allow_account_creation=False,
        discovery_pages={"/": "<html>no login form here</html>"},
        protected_path=None,
    )

    assert legacy_used == ["login"]
    assert len(session.login_calls) == 1  # legacy 경로로 폴백됨
    assert principals[0].extra["bootstrap_source"] == "provisioned"


def test_bootstrap_register_discovery_success_does_not_retry_registration_on_login_failure():
    # 등록은 discovery로 성공했는데 로그인 discovery만 실패하는 경우 — 재등록(session.bootstrap)
    # 하면 "이미 존재함" 에러가 나므로, legacy 로그인만 폴백으로 써야 한다(재등록 금지).
    session = _StubSession(
        {("POST", "/register"): _StubExchange(200, {"status": "success"})}
        # /login 에 대한 응답을 일부러 안 둬서 discover_login 이 실패하게 함
    )
    register_page = _LOGIN_PAGE.replace("loginForm", "registerForm").replace("/login", "/register")

    principals, legacy_used = bootstrap_principals(
        session,
        _target(["attacker_A"]),
        None,
        allow_account_creation=True,
        discovery_pages={"/register": register_page},
        protected_path=None,
    )

    assert legacy_used == ["login"]  # "register" 는 legacy 로 안 들어감(discovery로 성공했으므로)
    assert not session.bootstrap_calls  # 재등록 시도 안 함
    assert len(session.login_calls) == 1  # legacy 로그인만 폴백으로 씀
    assert principals[0].extra["bootstrap_source"] == "self_created"
