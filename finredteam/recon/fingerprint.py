"""수동적(passive) 관측 — 상태를 바꾸는 요청은 절대 보내지 않는다.

GET/OPTIONS 만 사용한다. discovery.py 가 나열한 후보 각각에 대해 익명/인증
상태 코드와 허용 메서드(OPTIONS 의 Allow 헤더)를 관측한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from finredteam.models.core import Endpoint, Principal
from finredteam.probing.session import Session, SessionError


@dataclass
class FingerprintObservation:
    endpoint: Endpoint
    anon_status: int | None = None
    authed_status: int | None = None
    allowed_methods: list[str] = field(default_factory=list)


def probe(session: Session, endpoint: Endpoint, principal: Principal | None) -> FingerprintObservation:
    obs = FingerprintObservation(endpoint=endpoint)

    try:
        ex = session.anonymous_request("GET", endpoint.path)
        obs.anon_status = ex.status_code
    except SessionError:
        pass

    if principal is not None and principal.token:
        try:
            ex = session.authed_request("GET", endpoint.path, principal)
            obs.authed_status = ex.status_code
        except SessionError:
            pass

    try:
        ex = session.anonymous_request("OPTIONS", endpoint.path)
        allow = ex.response_headers.get("allow") or ex.response_headers.get("Allow")
        if allow:
            obs.allowed_methods = [m.strip() for m in allow.split(",") if m.strip()]
    except SessionError:
        pass

    return obs


def probe_all(
    session: Session, endpoints: list[Endpoint], principal: Principal | None
) -> list[FingerprintObservation]:
    return [probe(session, ep, principal) for ep in endpoints]
