"""§5 ENTITLEMENT_RULES — 정상 경로로만 entitled_* 를 확정한다 (결정적, 우선순위 순).

1. self_created  — 우리가 회원가입/계좌개설로 만든 것 (가장 강한 근거)
2. explicit_owner_field — 서버 응답의 owner 필드 == 요청 주체
3. normal_flow — 로그인 직후 대시보드 등 기본 흐름에서 반환된 객체

Day3 범위: vuln-bank 는 owner_id 를 응답에 명시하지 않으므로(§ configs/vulnbank.yaml
entitlement.owner_field: null) self_created 만 구현한다. explicit_owner_field 는
어댑터 설정에 owner_field 가 있고 관측 응답에 그 필드가 실제로 존재할 때만 동작하도록
틀만 만들어 둔다 — 이 타겟에서는 자연히 스킵된다.
"""
from __future__ import annotations

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Principal
from finredteam.models.knowledge import Entitlement
from finredteam.targets.loader import TargetConfig


def from_self_created(principals: list[Principal]) -> list[Entitlement]:
    """회원가입으로 만든 자기 계좌 → entitled_own (source=self_created)."""
    return [
        Entitlement(
            principal=p.id,
            relation="entitled_own",
            obj=f"account:{p.account_number}",
            source="self_created",
            evidence_ref=None,
        )
        for p in principals
        if p.account_number
    ]


def from_explicit_owner_field(
    principal: Principal, obj: str, response_body: dict, owner_field: str | None
) -> Entitlement | None:
    """응답에 owner_field 가 있고 그 값이 principal 자신을 가리키면 entitled_own."""
    if not owner_field or not isinstance(response_body, dict):
        return None
    owner_value = response_body.get(owner_field)
    if owner_value is None:
        return None
    if str(owner_value) not in (principal.id, principal.username, principal.account_number):
        return None
    return Entitlement(
        principal=principal.id,
        relation="entitled_own",
        obj=obj,
        source="explicit_owner_field",
        evidence_ref=None,
    )


def discover(principals: list[Principal], graph: KnowledgeGraph, target: TargetConfig) -> list[Entitlement]:
    """우선순위대로 확정 가능한 entitlement 를 채우고 그래프에 반영한다.

    normal_flow(§5 규칙 3) 는 recon/mapper.py 가 fingerprint 로 얻은 대시보드 등
    응답을 넘겨줄 때 확장한다 — 여기서는 self_created 까지만 다룬다.
    """
    ents = from_self_created(principals)
    graph.add_entitlements(ents)
    return ents
