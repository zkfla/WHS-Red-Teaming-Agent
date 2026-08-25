"""정찰 오케스트레이션 — discovery → fingerprint → entitlement 를 순서대로 실행해
그래프와 사실 기반을 채운다. `agent/react.py` 가 BOOTSTRAP 직후 Phase 2/3 에서
호출한다.

seed_paths(정상 흐름에 필요한 최소한만 설정에 둔다 — §Black-box 취지)만으로는
표면이 좁으므로, 응답 본문에서 발견된 경로를 1홉만 추가로 fingerprint 한다
(무한 크롤은 아니다 — 안전상 상한을 둔다). 더 넓은 탐색은 ReAct 의
`discover_surface` 액션이 실행 중에 이어서 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from finredteam.config import MAX_PROBES
from finredteam.knowledge import inference
from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Endpoint, Principal
from finredteam.models.knowledge import Entitlement, Fact
from finredteam.probing.session import Session
from finredteam.recon import discovery, entitlement, fingerprint
from finredteam.targets.loader import TargetConfig


@dataclass
class ReconResult:
    endpoints: list[Endpoint]
    facts: list[Fact]
    entitlements: list[Entitlement]


def run(
    session: Session, target: TargetConfig, principals: list[Principal], graph: KnowledgeGraph
) -> ReconResult:
    endpoints = discovery.run(target)
    primary = principals[0] if principals else None

    facts: list[Fact] = []
    seen_paths = {ep.path for ep in endpoints}
    discovered_paths: set[str] = set()

    for ep in endpoints:
        obs = fingerprint.probe(session, ep, primary)
        facts.extend(inference.derive(obs))
        discovered_paths |= obs.discovered_paths - seen_paths

    # 1홉 추가 탐색 — 응답 본문에서 발견된 경로를 마저 찔러본다(무한 크롤 금지, 상한 적용).
    extra_paths = sorted(discovered_paths)[:MAX_PROBES]
    for path in extra_paths:
        extra_endpoint = Endpoint(method="GET", path=path, source="crawl")
        obs = fingerprint.probe(session, extra_endpoint, primary)
        facts.extend(inference.derive(obs))
        endpoints.append(extra_endpoint)
        seen_paths.add(path)

    ents = entitlement.discover(principals, graph, target)

    return ReconResult(endpoints=endpoints, facts=facts, entitlements=ents)
