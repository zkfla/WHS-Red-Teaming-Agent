"""관측(fingerprint 결과 등)에서 재사용 가능한 `Fact` 를 유도한다.

여기서 만드는 Fact 는 전부 `confidence="observed"` 다 — 실제로 응답을 받아서
확인한 것만 다룬다. 추측은 이 모듈의 몫이 아니다.
"""
from __future__ import annotations

from typing import Protocol

from finredteam.models.knowledge import Fact


class _EndpointLike(Protocol):
    path: str


class ObservationLike(Protocol):
    """recon/fingerprint.py 의 FingerprintObservation 이 만족하는 최소 형태.

    knowledge/ 는 recon/ 보다 먼저 만들어지는 계층이라(README 모듈 의존 순서),
    구체 타입 대신 이 구조만 요구한다 — recon/ 을 import 하지 않는다.
    """

    endpoint: _EndpointLike
    anon_status: int | None
    authed_status: int | None
    allowed_methods: list[str]


def derive(obs: ObservationLike) -> list[Fact]:
    facts: list[Fact] = []
    path = obs.endpoint.path

    if obs.anon_status is not None:
        facts.append(
            Fact(subject=path, predicate="anon_status", object=str(obs.anon_status), source="fingerprint")
        )
        if obs.anon_status not in (401, 403) and obs.anon_status < 400:
            facts.append(Fact(subject=path, predicate="requires_auth", object="false", source="fingerprint"))
        elif obs.authed_status is not None and obs.authed_status < 400 and obs.anon_status in (401, 403):
            facts.append(Fact(subject=path, predicate="requires_auth", object="true", source="fingerprint"))

    if obs.allowed_methods:
        facts.append(
            Fact(
                subject=path,
                predicate="allowed_methods",
                object=",".join(sorted(obs.allowed_methods)),
                source="fingerprint",
            )
        )

    return facts
