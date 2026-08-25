"""엔드포인트 후보 나열 — `targets/configs/*.yaml` 의 `discovery.seed_paths` 만 읽는다.

openapi/graphql introspection 은 대상마다 형식이 달라(§12 loader.py 참고) 2차
확장 지점으로 남긴다. Day3 범위는 seed_paths 기반 나열 + fingerprint 로 실제
반응을 확인하는 것까지다.
"""
from __future__ import annotations

from finredteam.models.core import Endpoint
from finredteam.targets.loader import TargetConfig


def run(target: TargetConfig) -> list[Endpoint]:
    disc = target.discovery_config()
    seed_paths = disc.get("seed_paths", []) if isinstance(disc, dict) else []
    return [Endpoint(method="GET", path=path, source="seed") for path in seed_paths]
