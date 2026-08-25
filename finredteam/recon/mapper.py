"""정찰 오케스트레이션 — discovery → fingerprint → entitlement 를 순서대로 실행해
그래프와 사실 기반을 채운다. `agent/loop.py`(Day6)가 Phase 2/3 에서 호출한다.
"""
from __future__ import annotations

from dataclasses import dataclass

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
    for ep in endpoints:
        obs = fingerprint.probe(session, ep, primary)
        facts.extend(inference.derive(obs))

    ents = entitlement.discover(principals, graph, target)

    return ReconResult(endpoints=endpoints, facts=facts, entitlements=ents)
