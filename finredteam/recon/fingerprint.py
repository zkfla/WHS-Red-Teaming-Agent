"""수동적(passive) 관측 — 상태를 바꾸는 요청은 절대 보내지 않는다.

GET/OPTIONS 만 사용한다. discovery.py 가 나열한 후보 각각에 대해 익명/인증
상태 코드와 허용 메서드(OPTIONS 의 Allow 헤더)를 관측한다.

응답 본문에서 다른 경로가 언급되어 있으면(HTML href/action, JSON 문자열 값)
그것도 뽑아낸다 — `targets/configs/*.yaml` 의 `discovery.seed_paths` 를
최소한으로만 유지해도(§ Black-box 취지), 정상적으로 앱을 한 번 쓰면 보이는
링크들로 어느 정도는 스스로 표면을 넓히기 위해서다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from finredteam.models.core import Endpoint, Principal
from finredteam.probing.session import Session, SessionError

# 따옴표 안의 "/..." 문자열(JSON 값) 과 href="/..."·action="/..." (HTML) 을 함께 잡는다.
_PATH_IN_TEXT_RE = re.compile(r'(?:href|action)?="(/(?![/])[A-Za-z0-9_\-./]{1,80})"')


def extract_discovered_paths(text: str) -> set[str]:
    if not text:
        return set()
    return {m for m in _PATH_IN_TEXT_RE.findall(text) if len(m) > 1}


@dataclass
class FingerprintObservation:
    endpoint: Endpoint
    anon_status: int | None = None
    authed_status: int | None = None
    allowed_methods: list[str] = field(default_factory=list)
    discovered_paths: set[str] = field(default_factory=set)


def probe(session: Session, endpoint: Endpoint, principal: Principal | None) -> FingerprintObservation:
    obs = FingerprintObservation(endpoint=endpoint)

    try:
        ex = session.anonymous_request("GET", endpoint.path)
        obs.anon_status = ex.status_code
        obs.discovered_paths |= extract_discovered_paths(ex.response_text)
    except SessionError:
        pass

    if principal is not None and principal.token:
        try:
            ex = session.authed_request("GET", endpoint.path, principal)
            obs.authed_status = ex.status_code
            obs.discovered_paths |= extract_discovered_paths(ex.response_text)
        except SessionError:
            pass

    try:
        ex = session.anonymous_request("OPTIONS", endpoint.path)
        allow = ex.response_headers.get("allow") or ex.response_headers.get("Allow")
        if allow:
            obs.allowed_methods = [m.strip() for m in allow.split(",") if m.strip()]
    except SessionError:
        pass

    obs.discovered_paths.discard(endpoint.path)
    return obs


def probe_all(
    session: Session, endpoints: list[Endpoint], principal: Principal | None
) -> list[FingerprintObservation]:
    return [probe(session, ep, principal) for ep in endpoints]
