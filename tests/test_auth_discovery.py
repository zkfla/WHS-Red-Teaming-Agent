"""Authentication Discovery(§ recon/auth_discovery.py) — 스텁 Session으로 네트워크
없이 검증한다. 픽스처는 실측(2026-08-26, vuln-bank 라이브 확인)과 동일한 모양이다:
로그인/회원가입 폼이 둘 다 password 필드를 가져서 서로 헷갈릴 수 있다는 것까지 포함.
"""
from __future__ import annotations

from types import SimpleNamespace

from finredteam.engagement.schema import EngagementCredential
from finredteam.probing.session import SessionError
from finredteam.recon import auth_discovery

_LOGIN_PAGE = """
<form id="loginForm">
    <input type="text" name="username">
    <input type="password" name="password">
</form>
<script>
document.getElementById('loginForm').addEventListener('submit', async (event) => {
    const response = await fetch('/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({})});
});
</script>
"""

_REGISTER_PAGE = """
<form id="registerForm">
    <input type="text" name="username">
    <input type="password" name="password">
</form>
<script>
document.getElementById('registerForm').addEventListener('submit', async (event) => {
    const response = await fetch('/register', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({})});
});
</script>
"""

_PAGES = {"/login": _LOGIN_PAGE, "/register": _REGISTER_PAGE}


class _StubExchange:
    def __init__(self, status_code: int, response_body: object = None):
        self.status_code = status_code
        self.response_body = response_body


class _StubSession:
    """`anonymous_request`/`request_with_header` 호출을 미리 정해둔 응답으로 스텁한다."""

    def __init__(self, responses: dict[tuple[str, str], _StubExchange]):
        self._responses = responses
        self.calls: list[tuple] = []

    def anonymous_request(self, method, path, json_body=None):
        self.calls.append(("anon", method, path, json_body))
        key = (method, path)
        if key not in self._responses:
            raise SessionError(f"unexpected call: {key}")
        return self._responses[key]

    def request_with_header(self, method, path, header_name, header_value):
        self.calls.append(("header", method, path, header_name, header_value))
        key = (method, path)
        if key not in self._responses:
            raise SessionError(f"unexpected call: {key}")
        return self._responses[key]


_CRED = EngagementCredential(username="alice", password="pw1")


# ── discover_login ───────────────────────────────────────────────────


def test_discover_login_picks_login_form_not_register_form():
    session = _StubSession(
        {
            ("POST", "/login"): _StubExchange(200, {"token": "tok123", "accountNumber": "42"}),
            ("GET", "/dashboard"): _StubExchange(200),  # Bearer 스킴 검증 통과
        }
    )
    auth = auth_discovery.discover_login(session, _PAGES, _CRED, protected_path="/dashboard")

    assert auth is not None
    assert auth.login_path == "/login"  # register 아님
    assert auth.username_field == "username"
    assert auth.password_field == "password"
    assert auth.token == "tok123"
    assert auth.account_number == "42"
    assert auth.header_value_template == "Bearer {token}"


def test_discover_login_returns_none_when_login_request_fails():
    session = _StubSession({("POST", "/login"): _StubExchange(400, {"message": "bad"})})
    assert auth_discovery.discover_login(session, _PAGES, _CRED, protected_path="/dashboard") is None


def test_discover_login_returns_none_when_no_token_in_response():
    session = _StubSession({("POST", "/login"): _StubExchange(200, {"status": "success"})})
    assert auth_discovery.discover_login(session, _PAGES, _CRED, protected_path="/dashboard") is None


def test_discover_login_falls_back_to_bearer_when_no_protected_path_to_verify():
    session = _StubSession({("POST", "/login"): _StubExchange(200, {"token": "tok123"})})
    auth = auth_discovery.discover_login(session, _PAGES, _CRED, protected_path=None)
    assert auth is not None
    assert auth.header_value_template == "Bearer {token}"  # 검증 못 하면 best-effort 관례값


def test_discover_login_tries_next_header_scheme_when_bearer_fails_verification():
    session = _StubSession(
        {
            ("POST", "/login"): _StubExchange(200, {"token": "tok123"}),
            ("GET", "/dashboard"): _StubExchange(401),  # Bearer 실패 -> 다음 스킴 시도해야 함
        }
    )
    # Bearer 는 실패하지만 검증할 다음 후보에 대한 응답이 없으므로(스텁 미정의) None 이어야 함
    assert auth_discovery.discover_login(session, _PAGES, _CRED, protected_path="/dashboard") is None


def test_discover_login_returns_none_on_no_password_form_at_all():
    session = _StubSession({})
    pages = {"/": "<html><body>no forms here</body></html>"}
    assert auth_discovery.discover_login(session, pages, _CRED) is None


def test_discover_login_extracts_nested_token():
    session = _StubSession(
        {
            ("POST", "/login"): _StubExchange(200, {"data": {"access_token": "nested-tok"}}),
            ("GET", "/dashboard"): _StubExchange(200),
        }
    )
    auth = auth_discovery.discover_login(session, _PAGES, _CRED, protected_path="/dashboard")
    assert auth is not None
    assert auth.token == "nested-tok"


# ── discover_register ────────────────────────────────────────────────


def test_discover_register_picks_register_form_not_login_form():
    session = _StubSession({("POST", "/register"): _StubExchange(200, {"status": "success"})})
    assert auth_discovery.discover_register(session, _PAGES, _CRED) is True


def test_discover_register_returns_false_on_failure():
    session = _StubSession({("POST", "/register"): _StubExchange(400, {"message": "exists"})})
    assert auth_discovery.discover_register(session, _PAGES, _CRED) is False


def test_discover_register_returns_false_when_no_form_found():
    session = _StubSession({})
    assert auth_discovery.discover_register(session, {"/": "<html></html>"}, _CRED) is False
