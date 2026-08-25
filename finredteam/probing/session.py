"""대상 애플리케이션과의 HTTP 세션 — 정상 인증 흐름(회원가입/로그인/인증요청)만 다룬다.

allowlist 검사(§16)와 Decimal 안전 JSON 파싱(§4 규칙 2)을 여기서 강제해서,
이후 모든 모듈이 이 세션을 통해서만 요청하면 두 규칙이 자동으로 지켜지게 한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from finredteam.config import REQUEST_TIMEOUT, assert_allowed_host
from finredteam.jsonpath import get_path
from finredteam.models.core import Principal
from finredteam.money import loads_money_json
from finredteam.storage.evidence import CapturedResponse
from finredteam.targets.loader import TargetConfig

logger = logging.getLogger(__name__)


class SessionError(Exception):
    """정상 흐름(회원가입/로그인 등)이 실패했을 때. 시험 실행 실패와는 구분한다."""


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


def _header_from_template(template: str, token: str) -> tuple[str, str]:
    name, _, rest = template.partition(":")
    return name.strip(), rest.strip().format(token=token)


class Session:
    """대상 애플리케이션과의 HTTP 세션. allowlist·timeout·Decimal 안전 파싱을 강제한다."""

    def __init__(self, target: TargetConfig) -> None:
        self.target = target
        self.base_url = target.adapter.base_url
        assert_allowed_host(self.base_url)
        self._client = httpx.Client(base_url=self.base_url, timeout=REQUEST_TIMEOUT)

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
        assert_allowed_host(f"{self.base_url}{path}")
        resp = self._client.request(method, path, json=json_body, headers=headers)
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
        )

    def register(self, principal_id: str, username: str, password: str) -> Principal:
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

    def authed_request(
        self,
        method: str,
        path: str,
        principal: Principal,
        json_body: dict[str, Any] | None = None,
    ) -> RawExchange:
        if not principal.token:
            raise SessionError(f"{principal.id} 에 토큰이 없다 — login 을 먼저 호출하라")
        header_name, header_value = _header_from_template(
            self.target.adapter.auth.token_header, principal.token
        )
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
        header_name, header_value = _header_from_template(
            self.target.adapter.auth.token_header, token
        )
        return self._request(method, path, json_body=json_body, headers={header_name: header_value})

    def capture(self, ex: RawExchange) -> CapturedResponse:
        return CapturedResponse(
            status_code=ex.status_code, body=ex.response_body, text=ex.response_text
        )
