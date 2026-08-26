"""HTML form + 연결된 JS 핸들러에서 endpoint/method/field 를 발견한다 — Auth/Operation
Discovery(recon/auth_discovery.py 등)가 공유하는 primitive.

★ best-effort 다 — 완전한 JS 파서가 아니라 표준 `html.parser.HTMLParser` + 정규식
기반이다. 많은 실제 웹앱이 `<form>`(정적 HTML) + 별도 정적 JS 파일의 `fetch()` 호출
(JS가 submit 을 가로채 요청을 만드는 패턴)로 되어 있는데, 이 정적 JS 파일 자체가
이미 GET 으로 받아지는 공개 리소스라서(`recon/discover_surface` 가 실제로
`swagger-ui-bundle.js` 같은 것도 받아온 전례가 있다) 소스코드를 읽지 않고도
black-box HTTP 관측만으로 이 정보를 찾을 수 있다(2026-08-26, vulnbank 실측 확인 —
아래 각 함수 docstring 참고).

여기서 못 찾으면 예외 없이 `None`/빈 리스트를 돌려준다 — 이 모듈이 실패해도 호출부가
LEGACY_BOOTSTRAP(사람이 작성한 `targets/configs/<name>.yaml`)로 안전하게 폴백할 수
있어야 한다(§11).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class FormField:
    name: str
    type: str = "text"  # HTML input type: text/password/email/number/hidden/...


@dataclass
class DiscoveredForm:
    form_id: str | None
    action: str | None  # <form action="..."> 가 있으면(고전적 HTML form 제출 방식)
    method: str | None  # <form method="..."> 가 있으면 — 요즘 앱은 대개 없다(JS가 처리)
    fields: list[FormField] = field(default_factory=list)

    def has_password_field(self) -> bool:
        return any(f.type == "password" for f in self.fields)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[DiscoveredForm] = []
        self._current: DiscoveredForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: v for k, v in attrs if v is not None}
        if tag == "form":
            self._current = DiscoveredForm(
                form_id=attr_dict.get("id"),
                action=attr_dict.get("action"),
                method=(attr_dict.get("method") or "").upper() or None,
            )
            self.forms.append(self._current)
        elif tag in ("input", "textarea", "select") and self._current is not None:
            name = attr_dict.get("name")
            if name:
                self._current.fields.append(FormField(name=name, type=attr_dict.get("type", "text")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def parse_forms(html: str) -> list[DiscoveredForm]:
    """페이지 하나에 있는 `<form>` 전부와 그 안의 `<input>/<textarea>/<select name=...>`
    필드명을 뽑는다. `HTMLParser`는 깨진/중첩된 HTML에도 비교적 관대해서, 실패해도
    예외를 던지지 않고 그때까지 파싱된 결과를 돌려준다."""
    parser = _FormParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.forms


@dataclass
class DiscoveredHandler:
    endpoint: str
    method: str
    auth_header_template: str | None = None  # 예: "'Bearer ' + token" 원문(사람이 읽을 참고용)
    uses_form_data: bool = False  # true 면 요청 body 필드명이 form 의 name= 그대로


_LISTENER_CALL_RE = re.compile(
    r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.addEventListener\(\s*['\"]submit['\"]\s*,\s*"
)
# `addEventListener('submit', X)` 의 X 가 취할 수 있는 두 형태:
# ① 이름 있는 함수 참조 — X = handlerName (실측: dashboard.js `handleTransfer`)
# ② 인라인 익명 함수 — X = function(e){...} / (e)=>{...} / async (e)=>{...}
#    (실측: login.html — 별도 이름 없이 그 자리에서 바로 정의됨)
_NAMED_REF_RE = re.compile(r"(\w+)\s*\)")
_INLINE_FUNC_START_RE = re.compile(r"(?:async\s*)?function\s*\([^)]*\)\s*\{")
_INLINE_ARROW_START_RE = re.compile(r"(?:async\s*)?\([^)]*\)\s*=>\s*\{")
_FUNCTION_DEF_RE_TEMPLATE = r"(?:async\s+)?function\s+{name}\s*\([^)]*\)\s*\{{"
_FETCH_CALL_RE = re.compile(r"fetch\(\s*[`'\"]([^`'\"]+)[`'\"]\s*,\s*(\{)")
_METHOD_RE = re.compile(r"method\s*:\s*['\"](\w+)['\"]")
_AUTH_HEADER_RE = re.compile(r"['\"]?Authorization['\"]?\s*:\s*([^,\n}]+)")
_FORM_DATA_RE = re.compile(r"new\s+FormData\(")


def _extract_balanced(text: str, open_idx: int, open_ch: str, close_ch: str) -> str | None:
    """`text[open_idx]`가 `open_ch`라고 가정하고, 짝이 맞는 `close_ch`까지의 내용
    (양쪽 괄호 제외)을 돌려준다 — 중첩 괄호를 정규식으로는 정확히 못 세므로 직접 센다."""
    if open_idx >= len(text) or text[open_idx] != open_ch:
        return None
    depth = 1
    i = open_idx + 1
    while i < len(text) and depth > 0:
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
        i += 1
    return text[open_idx + 1 : i - 1] if depth == 0 else None


def find_handler_name(form_id: str, js_text: str) -> str | None:
    """`addEventListener('submit', HANDLER)` 에서 HANDLER 가 **이름 있는 함수 참조**일
    때만 그 이름을 돌려준다(실측: dashboard.js `handleTransfer`). 인라인 익명 함수인
    경우(실측: login.html)는 이름이 없으므로 `None` — 그 경우는
    `_find_handler_body_for_form`이 바로 본문을 추출한다."""
    for m in _LISTENER_CALL_RE.finditer(js_text):
        if m.group(1) != form_id:
            continue
        rest = js_text[m.end() :]
        if _INLINE_FUNC_START_RE.match(rest) or _INLINE_ARROW_START_RE.match(rest):
            continue
        named_m = _NAMED_REF_RE.match(rest)
        if named_m:
            return named_m.group(1)
    return None


def extract_function_body(js_text: str, function_name: str) -> str | None:
    """`function NAME(...) { ... }`(또는 `async function`)의 본문만 뽑는다.
    중첩 중괄호를 직접 세서 짝을 맞춘다(정규식만으로는 불가능)."""
    m = re.search(_FUNCTION_DEF_RE_TEMPLATE.format(name=re.escape(function_name)), js_text)
    if not m:
        return None
    return _extract_balanced(js_text, m.end() - 1, "{", "}")


def find_fetch_call(js_body: str) -> DiscoveredHandler | None:
    """함수 본문 안에서 `fetch(url, {method: '...', headers: {...}, body: ...})`
    (실측: `fetch('/transfer', {method: 'POST', headers: {'Authorization': 'Bearer '
    + localStorage.getItem('jwt_token'), ...}, body: JSON.stringify(jsonData)})`)
    패턴을 찾아 endpoint/method/인증헤더/FormData 사용 여부를 뽑는다."""
    m = _FETCH_CALL_RE.search(js_body)
    if not m:
        return None
    endpoint = m.group(1)
    options_body = _extract_balanced(js_body, m.start(2), "{", "}")
    if options_body is None:
        return None
    method_m = _METHOD_RE.search(options_body)
    method = method_m.group(1).upper() if method_m else "GET"
    auth_m = _AUTH_HEADER_RE.search(options_body)
    auth_header_template = auth_m.group(1).strip() if auth_m else None
    uses_form_data = bool(_FORM_DATA_RE.search(js_body))
    return DiscoveredHandler(
        endpoint=endpoint, method=method, auth_header_template=auth_header_template, uses_form_data=uses_form_data
    )


def _find_handler_body_for_form(form_id: str, js_text: str, all_js_texts: list[str]) -> str | None:
    """`js_text` 안에서 `form_id`의 submit 리스너를 찾아, 그 핸들러 본문(js 텍스트)을
    돌려준다 — 이름 있는 함수 참조와 인라인 익명 함수 둘 다 지원한다(실측 두 사례
    모두 확인, 위 정규식 주석 참고)."""
    for m in _LISTENER_CALL_RE.finditer(js_text):
        if m.group(1) != form_id:
            continue
        rest = js_text[m.end() :]
        inline_m = _INLINE_FUNC_START_RE.match(rest) or _INLINE_ARROW_START_RE.match(rest)
        if inline_m:
            brace_idx = m.end() + inline_m.end() - 1  # rest 기준 '{' 위치를 js_text 기준으로 환산
            body = _extract_balanced(js_text, brace_idx, "{", "}")
            if body is not None:
                return body
            continue
        named_m = _NAMED_REF_RE.match(rest)
        if named_m:
            handler_name = named_m.group(1)
            for src in all_js_texts:
                body = extract_function_body(src, handler_name)
                if body is not None:
                    return body
    return None


def discover_form_handler(form_id: str, js_texts: list[str]) -> DiscoveredHandler | None:
    """`form_id` 를 다루는 JS 핸들러를 여러 JS 소스(인라인+외부 파일 전부 가능) 중에서
    찾아 fetch 호출까지 추적한다. 어느 단계든 실패하면 `None` — 예외 없음."""
    for js_text in js_texts:
        body = _find_handler_body_for_form(form_id, js_text, js_texts)
        if body is not None:
            result = find_fetch_call(body)
            if result is not None:
                return result
    return None
