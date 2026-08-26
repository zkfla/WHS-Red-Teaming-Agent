"""HTML form + JS 핸들러 스크래핑(§ recon/form_discovery.py).

두 가지 실측 패턴을 픽스처로 고정한다(2026-08-26, vuln-bank 라이브 확인):
① 인라인 익명 함수로 addEventListener 두 번째 인자를 바로 정의(login.html)
② 이름 있는 함수를 따로 등록하고 별도 파일에 정의(dashboard.js)
"""
from __future__ import annotations

from finredteam.recon.form_discovery import discover_form_handler, find_handler_name, parse_forms

# ── 실측 패턴 ① — 인라인 익명 함수 (vulnbank login.html 재현) ──────────

_LOGIN_HTML = """
<form id="loginForm">
    <input type="text" name="username" id="username" required>
    <input type="password" name="password" id="password" required>
</form>
<script>
document.addEventListener('DOMContentLoaded', function() {
    document.getElementById('loginForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const response = await fetch('/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: document.getElementById('username').value})
        });
    });
});
</script>
"""

# ── 실측 패턴 ② — 이름 있는 함수, 별도 JS 파일 (vulnbank dashboard.js 재현) ──

_DASHBOARD_HTML = """
<form id="transferForm">
    <input type="text" id="to_account" name="to_account" required>
    <input type="number" id="amount" name="amount" required>
    <textarea id="description" name="description"></textarea>
</form>
"""

_DASHBOARD_JS = """
document.getElementById('transferForm').addEventListener('submit', handleTransfer);
async function handleTransfer(event) {
    event.preventDefault();
    const formData = new FormData(event.target);
    const jsonData = {};
    formData.forEach((v, k) => jsonData[k] = v);
    const response = await fetch('/transfer', {
        method: 'POST',
        headers: {
            'Authorization': 'Bearer ' + localStorage.getItem('jwt_token'),
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(jsonData)
    });
}
"""


# ── parse_forms ──────────────────────────────────────────────────────


def test_parse_forms_extracts_field_names_and_types():
    forms = parse_forms(_LOGIN_HTML)
    assert len(forms) == 1
    assert forms[0].form_id == "loginForm"
    assert [(f.name, f.type) for f in forms[0].fields] == [("username", "text"), ("password", "password")]


def test_parse_forms_detects_password_field():
    forms = parse_forms(_LOGIN_HTML)
    assert forms[0].has_password_field() is True


def test_parse_forms_multiple_fields_textarea_included():
    forms = parse_forms(_DASHBOARD_HTML)
    assert len(forms) == 1
    names = [f.name for f in forms[0].fields]
    assert names == ["to_account", "amount", "description"]


def test_parse_forms_empty_html_returns_empty_list():
    assert parse_forms("<html><body>no forms here</body></html>") == []


def test_parse_forms_malformed_html_does_not_raise():
    # 닫는 태그 누락 등 — 예외 없이 최대한 파싱된 결과를 돌려줘야 한다.
    forms = parse_forms("<form id='x'><input name='a'>")
    assert forms and forms[0].fields[0].name == "a"


# ── find_handler_name / discover_form_handler ──────────────────────────


def test_find_handler_name_returns_none_for_inline_anonymous_handler():
    # 이름이 없는 인라인 함수는 이름을 못 돌려준다 — discover_form_handler 가
    # 별도 경로로 본문을 바로 추출해야 한다(아래 테스트).
    assert find_handler_name("loginForm", _LOGIN_HTML) is None


def test_find_handler_name_returns_name_for_named_reference():
    assert find_handler_name("transferForm", _DASHBOARD_JS) == "handleTransfer"


def test_discover_form_handler_inline_anonymous_function():
    handler = discover_form_handler("loginForm", [_LOGIN_HTML])
    assert handler is not None
    assert handler.endpoint == "/login"
    assert handler.method == "POST"
    assert handler.uses_form_data is False  # 이 픽스처는 FormData 를 안 씀


def test_discover_form_handler_named_function_in_separate_file():
    handler = discover_form_handler("transferForm", [_DASHBOARD_HTML, _DASHBOARD_JS])
    assert handler is not None
    assert handler.endpoint == "/transfer"
    assert handler.method == "POST"
    assert handler.uses_form_data is True
    assert "Bearer" in (handler.auth_header_template or "")


def test_discover_form_handler_returns_none_for_unknown_form_id():
    assert discover_form_handler("nonexistentForm", [_LOGIN_HTML, _DASHBOARD_JS]) is None


def test_discover_form_handler_returns_none_on_garbage_input():
    # 완전히 관계없는 텍스트 — 크래시 없이 None 이어야 한다(§ LEGACY_BOOTSTRAP 폴백 전제).
    assert discover_form_handler("loginForm", ["not even javascript {{{ ]"]) is None
