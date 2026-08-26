"""Operation/Parameter Discovery(§ recon/operation_discovery.py) — 픽스처는 실측
(2026-08-26, vuln-bank 라이브 확인)과 동일한 모양이다: `<form id="transferForm">`
(to_account/amount/description) + 별도 JS 파일의 이름 있는 핸들러."""
from __future__ import annotations

from decimal import Decimal

from finredteam.recon.operation_discovery import discover_money_operation, discover_self_balance

_TRANSFER_HTML = """
<div class="account-card-balance" id="balance">$900.00</div>
<form id="transferForm">
    <input type="text" id="to_account" name="to_account" required>
    <input type="number" id="amount" name="amount" required>
    <textarea id="description" name="description"></textarea>
</form>
"""

_TRANSFER_JS = """
document.getElementById('transferForm').addEventListener('submit', handleTransfer);
async function handleTransfer(event) {
    const formData = new FormData(event.target);
    const jsonData = {};
    formData.forEach((v, k) => jsonData[k] = v);
    const response = await fetch('/transfer', {
        method: 'POST',
        headers: {'Authorization': 'Bearer ' + localStorage.getItem('jwt_token')},
        body: JSON.stringify(jsonData)
    });
}
"""


def test_discover_money_operation_finds_amount_and_recipient_fields():
    op = discover_money_operation({"/dashboard": _TRANSFER_HTML, "/static/dashboard.js": _TRANSFER_JS})
    assert op is not None
    assert op.endpoint == "/transfer"
    assert op.method == "POST"
    assert op.amount_field == "amount"
    assert op.recipient_field == "to_account"
    assert op.other_fields == ["description"]


def test_discover_money_operation_returns_none_without_js_handler():
    # HTML만 있고 핸들러를 찾을 JS가 없으면(엔드포인트를 모르므로) None 이어야 한다.
    op = discover_money_operation({"/dashboard": _TRANSFER_HTML})
    assert op is None


def test_discover_money_operation_returns_none_when_no_money_like_form():
    html = "<form id='bioForm'><textarea name='bio'></textarea></form>"
    assert discover_money_operation({"/dashboard": html}) is None


def test_discover_money_operation_ignores_unrelated_forms():
    other_form = "<form id='searchForm'><input type='text' name='query'></form>"
    op = discover_money_operation(
        {"/dashboard": _TRANSFER_HTML + other_form, "/static/dashboard.js": _TRANSFER_JS}
    )
    assert op is not None
    assert op.endpoint == "/transfer"  # 관계없는 폼(query 필드)은 후보에서 걸러짐


# ── discover_self_balance ────────────────────────────────────────────


def test_discover_self_balance_extracts_dollar_amount():
    assert discover_self_balance(_TRANSFER_HTML) == Decimal("900.00")


def test_discover_self_balance_handles_comma_thousands():
    html = '<div id="balance">$1,234.56</div>'
    assert discover_self_balance(html) == Decimal("1234.56")


def test_discover_self_balance_returns_none_when_no_balance_element():
    assert discover_self_balance("<div>nothing here</div>") is None


def test_discover_self_balance_matches_class_not_just_id():
    html = '<span class="user-balance-display">$42.00</span>'
    assert discover_self_balance(html) == Decimal("42.00")
