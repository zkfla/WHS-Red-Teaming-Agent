"""Authentication Discovery — 로그인 endpoint/method/필드 매핑/token 추출 방식/
인증 헤더 스킴을 Black-box로 발견한다(§ recon/form_discovery.py 기반).

사용자에게 받는 건 `EngagementCredential`(username/password)뿐이다 — "이 credential
을 어떻게 쓰는지"(endpoint/필드명/헤더 형식)는 여기서 직접 관측해 알아낸다(engagement
§3: "Credentials → 사용자 제공 가능 / Authentication Protocol → Agent가 발견").

성공하면 실제로 credential 로 로그인까지 완료한 상태(토큰 확보)를 돌려준다 — 이건
이미 허용된 "정상 인증 시도"지 프로빙이 아니다. 어느 단계든 실패하면 예외 없이
`None` — 호출부(agent/react.py)가 LEGACY_BOOTSTRAP(사람이 작성한 어댑터 YAML)으로
안전하게 폴백한다(§11).
"""
from __future__ import annotations

from dataclasses import dataclass

from finredteam.engagement.schema import EngagementCredential
from finredteam.probing.session import Session, SessionError
from finredteam.recon.form_discovery import DiscoveredForm, discover_form_handler, parse_forms

# 흔한 "username류" 필드명 우선순위(먼저 매칭되는 게 더 신뢰됨) — 이건 필드
# *이름표를 짓는* 게 아니라, 이미 발견된 필드들 중 어느 것이 사용자 식별자
# 자리인지 고르는 것뿐이다(비밀번호가 아닌 나머지 필드는 이거 아니면 답이 없다).
_USERNAME_FIELD_HINTS = ("username", "user", "email", "login")

_TOKEN_KEY_CANDIDATES = frozenset(
    {"token", "access_token", "accesstoken", "jwt", "auth_token", "authtoken", "session_token", "sessiontoken"}
)

_HEADER_SCHEME_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Authorization", "Bearer {token}"),
    ("Authorization", "{token}"),
    ("Cookie", "token={token}"),
)


@dataclass
class DiscoveredAuth:
    login_path: str
    method: str
    username_field: str
    password_field: str
    header_name: str
    header_value_template: str  # "{token}" 자리표시자 포함, 예: "Bearer {token}"
    token: str
    account_number: str | None = None


def _password_form_handlers(pages: dict[str, str]) -> list[tuple[DiscoveredForm, "object"]]:
    """password 타입 필드가 있는 폼 전부(=로그인/회원가입 후보, HTML5 관례)와 그
    발견된 핸들러를 짝지어 돌려준다. 한 대상에 로그인/회원가입 폼이 둘 다 있으면
    (실측: vulnbank) 여럿이 나올 수 있다 — 호출부가 endpoint 이름으로 골라낸다."""
    seen_form_ids: set[str] = set()
    pairs = []
    for html in pages.values():
        for form in parse_forms(html):
            if not form.form_id or not form.has_password_field() or form.form_id in seen_form_ids:
                continue
            seen_form_ids.add(form.form_id)
            handler = discover_form_handler(form.form_id, list(pages.values()))
            if handler is not None:
                pairs.append((form, handler))
    return pairs


def _find_login_form(pages: dict[str, str]) -> DiscoveredForm | None:
    """로그인 폼을 고른다 — endpoint 에 "regist"가 없는 걸 우선(로그인/회원가입
    폼이 둘 다 후보로 잡힐 수 있으므로, `discover_register`의 정반대 기준)."""
    pairs = _password_form_handlers(pages)
    pairs.sort(key=lambda fh: 1 if "regist" in fh[1].endpoint.lower() else 0)
    return pairs[0][0] if pairs else None


def _field_mapping(form: DiscoveredForm) -> tuple[str, str] | None:
    password_field = next((f.name for f in form.fields if f.type == "password"), None)
    if password_field is None:
        return None
    candidates = [f.name for f in form.fields if f.type != "password"]
    if not candidates:
        return None
    username_field = next(
        (n for hint in _USERNAME_FIELD_HINTS for n in candidates if hint in n.lower()), candidates[0]
    )
    return username_field, password_field


def _extract_token(body: object, _depth: int = 0) -> str | None:
    if not isinstance(body, dict) or _depth > 1:
        return None
    for key, value in body.items():
        if key.lower() in _TOKEN_KEY_CANDIDATES and isinstance(value, str) and value:
            return value
    for value in body.values():  # 흔한 "data": {"token": ...} 래핑 한 겹만 더 본다
        nested = _extract_token(value, _depth + 1)
        if nested is not None:
            return nested
    return None


def _extract_account_number(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("accountNumber", "account_number", "account_no"):
        value = body.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _resolve_header_scheme(session: Session, token: str, protected_path: str | None) -> tuple[str, str] | None:
    """`protected_path`(익명으로는 401/403이었던 걸로 이미 관측된 경로)가 있으면
    후보 스킴을 실제로 시도해서 200이 되는 것으로 검증한다. 없으면 검증할 방법이
    없으니 가장 흔한 관례(Bearer)를 best-effort로 채택한다."""
    if protected_path is None:
        name, template = _HEADER_SCHEME_CANDIDATES[0]
        return name, template
    for name, template in _HEADER_SCHEME_CANDIDATES:
        value = template.replace("{token}", token)
        try:
            ex = session.request_with_header("GET", protected_path, name, value)
        except SessionError:
            continue
        if ex.status_code < 400:
            return name, template
    return None


def discover_login(
    session: Session,
    pages: dict[str, str],
    credential: EngagementCredential,
    *,
    protected_path: str | None = None,
) -> DiscoveredAuth | None:
    """`pages`: 사전 크롤에서 모은 {path: html} — 정적 자산(JS)도 이 dict 하나에
    같이 넣어서 넘기면 된다(form_discovery 는 HTML/JS를 구분하지 않고 텍스트로만
    본다). `protected_path`: 익명 요청이 401/403이었던 걸로 이미 관측된 경로가
    있으면 넘긴다 — 헤더 스킴 검증에 쓰인다(없어도 동작은 하되 검증 없이
    best-effort)."""
    form = _find_login_form(pages)
    if form is None or form.form_id is None:
        return None

    mapping = _field_mapping(form)
    if mapping is None:
        return None
    username_field, password_field = mapping

    handler = discover_form_handler(form.form_id, list(pages.values()))
    if handler is None:
        return None

    body = {username_field: credential.username, password_field: credential.password}
    try:
        ex = session.anonymous_request(handler.method, handler.endpoint, json_body=body)
    except SessionError:
        return None
    if ex.status_code >= 400:
        return None

    token = _extract_token(ex.response_body)
    if token is None:
        return None

    scheme = _resolve_header_scheme(session, token, protected_path)
    if scheme is None:
        return None
    header_name, header_value_template = scheme

    return DiscoveredAuth(
        login_path=handler.endpoint,
        method=handler.method,
        username_field=username_field,
        password_field=password_field,
        header_name=header_name,
        header_value_template=header_value_template,
        token=token,
        account_number=_extract_account_number(ex.response_body),
    )


def discover_register(session: Session, pages: dict[str, str], credential: EngagementCredential) -> bool:
    """로그인과 같은 방식이지만 회원가입 폼(비밀번호 필드가 있는 폼 중 endpoint 에
    "regist"가 들어간 것 우선) 대상이다 — 발견되면 그 자리에서 실제로 계정을
    만든다(이미 허용된 "정상 계정 생성" 행위, `allow_account_creation` 정책 검사는
    호출부의 몫). 토큰은 안 돌려준다 — 성공하면 곧바로 `discover_login`을 같은
    credential 로 불러서 로그인하면 된다(회원가입 응답이 토큰을 주든 안 주든
    상관없이 항상 통하는 경로 하나로 통일)."""
    # endpoint 에 "regist"가 들어간 걸 우선한다(관례) — 로그인 폼과 같은 password
    # 필드 신호를 공유하므로 둘 다 후보에 잡힐 수 있어서다(`_find_login_form`과
    # 정반대 기준).
    handlers = _password_form_handlers(pages)
    handlers.sort(key=lambda fh: 0 if "regist" in fh[1].endpoint.lower() else 1)

    for form, handler in handlers:
        mapping = _field_mapping(form)
        if mapping is None:
            continue
        username_field, password_field = mapping
        body = {username_field: credential.username, password_field: credential.password}
        try:
            ex = session.anonymous_request(handler.method, handler.endpoint, json_body=body)
        except SessionError:
            continue
        if ex.status_code < 400:
            return True
    return False
