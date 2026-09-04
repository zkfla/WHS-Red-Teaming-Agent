"""대상 애플리케이션과의 HTTP 세션 — 정상 인증 흐름(회원가입/로그인/인증요청)만 다룬다.

Engagement 정책 검사(§8)와 Decimal 안전 JSON 파싱(§4 규칙 2)을 여기서
강제해서, 이후 모든 모듈이 이 세션을 통해서만 요청하면 두 규칙이 자동으로
지켜지게 한다. `_request()` 가 이 프로젝트에서 실제 네트워크로 나가는
유일한 지점이다 — 상위 모듈이 정책 검사를 깜빡해도 여기서 다시 막힌다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from finredteam.config import REQUEST_TIMEOUT
from finredteam.engagement.policy import CompiledPolicy
from finredteam.jsonpath import get_path
from finredteam.models.core import Principal
from finredteam.money import loads_money_json
from finredteam.storage.evidence import CapturedResponse
from finredteam.targets.loader import TargetConfig

logger = logging.getLogger(__name__)


class SessionError(Exception):
    """정상 흐름(회원가입/로그인 등)이 실패했을 때. 시험 실행 실패와는 구분한다."""


class _RateLimiter:
    """`policy.max_requests_per_second` 를 지키기 위한 단순 고정 간격 스로틀러.
    요청을 막지 않고 필요한 만큼만 sleep 한다 — 거부가 아니라 속도 제한이다."""

    def __init__(self, max_per_second: float) -> None:
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._last: float | None = None

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        if self._last is not None:
            remaining = self._min_interval - (now - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


@dataclass
class RawExchange:
    """요청/응답 원문 — storage/evidence.py 로 넘기기 전 중간 표현."""

    method: str
    url: str
    request_body: dict[str, Any] | None
    status_code: int
    response_body: Any
    response_text: str
    response_headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0


def _header_from_template(template: str, token: str) -> tuple[str, str]:
    name, _, rest = template.partition(":")
    return name.strip(), rest.strip().format(token=token)


class Session:
    """대상 애플리케이션과의 HTTP 세션. Engagement 정책·timeout·Decimal 안전 파싱을 강제한다."""

    def __init__(
        self, target: TargetConfig, policy: CompiledPolicy, *, _transport: httpx.BaseTransport | None = None
    ) -> None:
        """`_transport`: 테스트 전용 — `httpx.MockTransport` 를 주입해서 실제
        네트워크 없이 응답을 시뮬레이션한다(`tests/test_session.py`)."""
        self.target = target
        self.policy = policy
        self.base_url = target.adapter.base_url
        policy.check_request("GET", self.base_url)  # base_url 자체가 이미 정책 밖이면 즉시 실패
        # follow_redirects=False 를 명시한다 — 리다이렉트를 자동으로 따라가면 정책이 검사한
        # 호스트와 실제로 요청이 도달하는 호스트가 달라질 수 있다(§10). 이 프로젝트는
        # 리다이렉트를 절대 자동으로 쫓지 않는다 — 3xx 도 그냥 하나의 status_code 로 관측한다.
        self._client = httpx.Client(
            base_url=self.base_url, timeout=REQUEST_TIMEOUT, follow_redirects=False, transport=_transport
        )
        self._rate_limiter = _RateLimiter(policy.max_requests_per_second)
        # ★ Black-box Auth Discovery(recon/auth_discovery.py)가 찾은 헤더 스킴 —
        # 설정되면 `authed_request()`가 정적 `target.adapter.auth.token_header`
        # 보다 이걸 우선한다. 설정 안 하면(예: 어댑터 YAML 이 있고 discovery가
        # 실패해서 legacy 로그인으로 폴백한 경우) 기존처럼 정적 config 를 쓴다.
        self._discovered_header_template: str | None = None

    def set_discovered_auth_header(self, header_name: str, value_template: str) -> None:
        """`recon/auth_discovery.py::DiscoveredAuth.header_name/header_value_template`
        을 그대로 넘긴다 — 이후 모든 `authed_request()` 가 이 스킴을 쓴다."""
        self._discovered_header_template = f"{header_name}: {value_template}"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> RawExchange:
        # httpx.URL(base).join(path) 는 httpx.Client 가 base_url + path 를 실제로 어떻게
        # 합치는지(그리고 path 가 절대 URL이면 base_url 을 무시하고 그걸 그대로 쓴다는 것도)
        # 그대로 재현한다 — 예전엔 문자열을 그냥 이어붙여서 검사했는데, path 가 절대 URL이면
        # 검사 대상과 실제 요청 대상이 달라질 수 있는 허점이 있었다(§10).
        resolved_url = str(httpx.URL(self.base_url).join(path))
        self.policy.check_request(method, resolved_url)
        self._rate_limiter.wait()
        start = time.monotonic()
        try:
            resp = self._client.request(method, path, json=json_body, headers=headers)
        except httpx.RequestError as exc:
            # 전송 계층 실패(연결 거부·타임아웃·DNS 등)를 SessionError 로 변환한다 —
            # 상위(executor/burst)는 SessionError 만 잡으므로, 이렇게 하지 않으면
            # 실행 중 일시적 네트워크 장애 한 번에 전체 run 이 죽는다(§ sad path).
            raise SessionError(f"요청 실패: {type(exc).__name__}: {exc}") from exc
        elapsed_ms = (time.monotonic() - start) * 1000
        # 인증은 오직 Authorization 헤더로만 한다(authed_request). httpx 의 쿠키 잭이
        # 로그인 응답의 Set-Cookie 를 저장해두면, 이후 "익명" 요청에도 그 쿠키가 자동
        # 첨부되어 BOLA/anon_status 관측이 오염된다 — 대상 서버가 쿠키도 유효한 인증
        # 수단으로 받아주는 취약점(§ configs/vulnbank.yaml auth 주석)이 있을 때 특히 그렇다.
        # 매 요청 직후 즉시 비워 다음 요청에 절대 새지 않게 한다.
        self._client.cookies.clear()
        logger.debug("%s %s -> %d", method, path, resp.status_code)
        body: Any = None
        if resp.text:
            try:
                body = loads_money_json(resp.text)
            except ValueError:
                body = None
        return RawExchange(
            method=method,
            url=f"{self.base_url}{path}",
            request_body=json_body,
            status_code=resp.status_code,
            response_body=body,
            response_text=resp.text,
            response_headers=dict(resp.headers),
            elapsed_ms=elapsed_ms,
        )

    def register(self, principal_id: str, username: str, password: str) -> Principal:
        if self.target.adapter.auth is None:
            raise SessionError(
                "정적 어댑터 설정(auth)이 없음 — Black-box Auth Discovery 도 실패한 상태에서 "
                "회원가입을 시도함 (targets/configs/<name>.yaml 에 auth.register 를 직접 "
                "작성하거나, 이미 계정이 있다면 Engagement 에 credentials 로 제공하세요)"
            )
        cfg = self.target.adapter.auth.register_ep
        fields = {
            cfg.fields.get("username", "username"): username,
            cfg.fields.get("password", "password"): password,
        }
        ex = self._request(cfg.method, cfg.path, json_body=fields)
        if ex.status_code >= 400:
            raise SessionError(f"register 실패 ({ex.status_code}): {ex.response_text[:300]}")

        account_number = None
        if cfg.account_number_path:
            account_number = get_path(ex.response_body, cfg.account_number_path)
        return Principal(
            id=principal_id,
            username=username,
            password=password,
            account_number=str(account_number) if account_number is not None else None,
        )

    def login(self, principal: Principal) -> Principal:
        if self.target.adapter.auth is None:
            raise SessionError(
                "정적 어댑터 설정(auth)이 없음 — Black-box Auth Discovery 도 실패한 상태에서 "
                "로그인을 시도함 (targets/configs/<name>.yaml 에 auth.login 을 직접 작성하세요)"
            )
        cfg = self.target.adapter.auth.login
        fields = {
            cfg.fields.get("username", "username"): principal.username,
            cfg.fields.get("password", "password"): principal.password,
        }
        ex = self._request(cfg.method, cfg.path, json_body=fields)
        if ex.status_code >= 400:
            raise SessionError(f"login 실패 ({ex.status_code}): {ex.response_text[:300]}")

        token = get_path(ex.response_body, cfg.token_path) if cfg.token_path else None
        account_number = principal.account_number
        if cfg.account_number_path:
            observed = get_path(ex.response_body, cfg.account_number_path)
            if observed is not None:
                account_number = str(observed)
        return principal.model_copy(update={"token": token, "account_number": account_number})

    def bootstrap(self, principal_id: str, username: str, password: str) -> Principal:
        """회원가입 + 로그인. entitled_own(account) 의 self_created 근거가 된다 (§5)."""
        principal = self.register(principal_id, username, password)
        return self.login(principal)

    def create_virtual_card(self, principal: Principal, *, card_limit: str = "1000.00") -> str | None:
        """가상카드 생성. 카드 관련 BOLA(freeze/transactions/limit) 시험 대상을
        그래프에 entitled_own(card) 로 등록하려면 먼저 실존하는 카드가 있어야 한다
        — self_created 근거가 된다(계좌의 register 와 같은 역할). 카드 id 문자열을
        반환하거나, 실패하면 None(예외를 던지지 않는다 — 호출부가 UNKNOWN 등으로
        우아하게 처리하게 한다)."""
        try:
            ex = self.authed_request(
                "POST", "/api/virtual-cards/create", principal, json_body={"card_limit": card_limit}
            )
        except SessionError:
            return None
        if ex.status_code >= 400 or not isinstance(ex.response_body, dict):
            return None
        card_id = get_path(ex.response_body, "$.card_details.id")
        return str(card_id) if card_id is not None else None

    def authed_request(
        self,
        method: str,
        path: str,
        principal: Principal,
        json_body: dict[str, Any] | None = None,
    ) -> RawExchange:
        if not principal.token:
            raise SessionError(f"{principal.id} 에 토큰이 없다 — login 을 먼저 호출하라")
        template = self._discovered_header_template
        if template is None:
            if self.target.adapter.auth is None:
                raise SessionError(
                    "인증 헤더 스킴을 모름 — Black-box Auth Discovery 도 실패했고 정적 "
                    "어댑터 설정(auth.token_header)도 없음"
                )
            template = self.target.adapter.auth.token_header
        header_name, header_value = _header_from_template(template, principal.token)
        return self._request(method, path, json_body=json_body, headers={header_name: header_value})

    def anonymous_request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> RawExchange:
        """인증 없이 요청 — BOLA 등 인증 우회 시험용. 정상 흐름이 아님을 호출부가 인지해야 한다."""
        return self._request(method, path, json_body=json_body)

    def raw_token_request(
        self, method: str, path: str, token: str, json_body: dict[str, Any] | None = None
    ) -> RawExchange:
        """임의로 만든(위조) 토큰 문자열을 그대로 실어 보낸다 — JWT 위조 시험 전용.

        `authed_request` 는 정상 발급된 `Principal.token` 만 받는다. 이 메서드는
        정상 흐름이 아님을 명시적으로 드러내기 위해 이름을 다르게 뒀다.
        """
        template = self._discovered_header_template
        if template is None:
            if self.target.adapter.auth is None:
                raise SessionError("인증 헤더 스킴을 모름 — Auth Discovery 도 실패했고 정적 설정도 없음")
            template = self.target.adapter.auth.token_header
        header_name, header_value = _header_from_template(template, token)
        return self._request(method, path, json_body=json_body, headers={header_name: header_value})

    def request_with_header(
        self, method: str, path: str, header_name: str, header_value: str, json_body: dict[str, Any] | None = None
    ) -> RawExchange:
        """임의의 헤더 이름/값 하나로 요청한다 — `target.adapter.auth.token_header`
        (정적 config)를 거치지 않는다. `recon/auth_discovery.py`가 발견된 토큰을
        어떤 헤더 스킴(Bearer/raw/Cookie)으로 실어야 통하는지 **검증**할 때만 쓴다.
        읽기 전용 GET 대상에만 쓰는 걸 전제로 한다(정상 흐름이 아님을 명시적으로
        드러내기 위해 이름을 다르게 뒀다 — `raw_token_request`와 같은 이유)."""
        return self._request(method, path, json_body=json_body, headers={header_name: header_value})

    def capture(self, ex: RawExchange) -> CapturedResponse:
        return CapturedResponse(
            status_code=ex.status_code, body=ex.response_body, text=ex.response_text
        )
