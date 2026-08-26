"""Operation/Parameter Discovery — 인증된 페이지의 폼에서 금전 이동으로 보이는
operation(예: transfer)의 endpoint/method/필드명을 발견하고, 자기 잔액을 관측할
채널(렌더링된 HTML 텍스트)을 찾는다(§ recon/form_discovery.py 기반).

★ Human pre-analysis 제거 취지: `targets/configs/*.yaml`의 `baseline_ops`/
`observation.balance`처럼 사람이 정적으로 적어둔 값 대신, 로그인 후 정상적으로
보이는 페이지(대시보드류)를 관측해서 직접 찾는다. endpoint/method/필드명은
`confidence="observed"`(실제 폼에서 읽음), "이게 송금(transfer)이다"라는
이름표만 `confidence="inferred"`다(§6 — observed/inferred 분리, entitled_*/
observed_* 철학과 같은 정신).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from finredteam.recon.form_discovery import DiscoveredForm, discover_form_handler, parse_forms

# 필드 *이름표를 짓는* 게 아니라, 이미 발견된 필드들 중 "금액"/"수취인" 자리가
# 어느 것인지 고르는 것뿐이다 — 관례적인 이름 매칭(휴리스틱, best-effort).
_AMOUNT_FIELD_HINTS = ("amount", "value", "sum", "quantity")
_RECIPIENT_FIELD_HINTS = ("to_account", "recipient", "destination", "payee", "to", "account")

# 실측: vulnbank `<div class="account-card-balance" id="balance">$900.00</div>` —
# id/class 이름에 "balance"가 있는 엘리먼트의 텍스트에서 숫자를 뽑는다.
_BALANCE_ELEMENT_RE = re.compile(
    r'(?:id|class)="[^"]*balance[^"]*"[^>]*>\s*\$?\s*([\d][\d,]*\.?\d*)', re.IGNORECASE
)


@dataclass
class DiscoveredOperation:
    endpoint: str
    method: str
    amount_field: str
    recipient_field: str
    other_fields: list[str]


def _money_field_mapping(form: DiscoveredForm) -> tuple[str, str] | None:
    names = [f.name for f in form.fields]
    amount_field = next((n for hint in _AMOUNT_FIELD_HINTS for n in names if hint in n.lower()), None)
    recipient_field = next(
        (n for hint in _RECIPIENT_FIELD_HINTS for n in names if hint in n.lower() and n != amount_field), None
    )
    if amount_field is None or recipient_field is None:
        return None
    return amount_field, recipient_field


def discover_money_operation(pages: dict[str, str]) -> DiscoveredOperation | None:
    """금액류 + 수취인류 필드가 함께 있는 폼을 "송금형 operation" 후보로 본다
    (실측: vulnbank `<form id="transferForm">` — to_account/amount/description).
    여러 폼이 후보에 걸리면 필드 수가 적어(가장 단순해) 신뢰도가 높은 것을 우선한다."""
    candidates: list[tuple[DiscoveredForm, tuple[str, str]]] = []
    for html in pages.values():
        for form in parse_forms(html):
            if not form.form_id:
                continue
            mapping = _money_field_mapping(form)
            if mapping is not None:
                candidates.append((form, mapping))
    candidates.sort(key=lambda fc: len(fc[0].fields))

    for form, (amount_field, recipient_field) in candidates:
        handler = discover_form_handler(form.form_id, list(pages.values()))
        if handler is None:
            continue
        other = [f.name for f in form.fields if f.name not in (amount_field, recipient_field)]
        return DiscoveredOperation(
            endpoint=handler.endpoint,
            method=handler.method,
            amount_field=amount_field,
            recipient_field=recipient_field,
            other_fields=other,
        )
    return None


def discover_self_balance(html: str) -> Decimal | None:
    """렌더링된 페이지 텍스트에서 "잔액으로 보이는" 값을 찾는다 — best-effort,
    못 찾으면 예외 없이 None."""
    m = _BALANCE_ELEMENT_RE.search(html)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
