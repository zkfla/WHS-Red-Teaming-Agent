"""knowledge/graph.py 단위 테스트.

★ 가장 중요한 불변식(§5): observed_* 관측은 어떤 경로로도 entitled_* 를
추가하거나 바꾸지 않는다. "조회가 됐다"는 사실이 권한의 근거가 되어 BOLA를
스스로 정당화해버리는 v3의 문제를 여기서 코드로 막는다.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.knowledge import Entitlement


def test_entitled_relation_literal_rejects_observed_prefix():
    # Entitlement 모델 자체가 entitled_* 이외의 relation 을 거부한다 (pydantic Literal).
    with pytest.raises(ValidationError):
        Entitlement(
            principal="attacker_A",
            relation="observed_read",  # type: ignore[arg-type]
            obj="account:777",
            source="self_created",
        )


def test_observed_never_promotes_to_entitled():
    graph = KnowledgeGraph()

    # attacker_A 가 account:777 (자기 것이 아님) 을 여러 번 성공적으로 조회했다고 해도
    graph.add_observed("attacker_A", "account:777", "read")
    graph.add_observed("attacker_A", "account:777", "read")
    graph.add_observed("attacker_A", "account:777", "read")

    # entitled_read 는 절대 생기지 않는다.
    assert graph.has_entitlement("attacker_A", "account:777", "read") is False
    assert graph.entitlement_unknown("account:777") is True
    assert len(graph.entitled_edges()) == 0
    assert len(graph.observed_edges()) == 3


def test_add_observed_rejects_entitled_relation_string():
    graph = KnowledgeGraph()
    with pytest.raises(ValueError):
        graph.add_observed("attacker_A", "account:777", "entitled_read")


def test_entitled_own_satisfies_read_and_write_lookup():
    graph = KnowledgeGraph()
    graph.add_entitlement(
        Entitlement(
            principal="attacker_A", relation="entitled_own", obj="account:111", source="self_created"
        )
    )
    assert graph.has_entitlement("attacker_A", "account:111", "read") is True
    assert graph.has_entitlement("attacker_A", "account:111", "write") is True
    assert graph.has_entitlement("attacker_A", "account:999", "read") is False


def test_entitlement_unknown_true_only_without_any_entitled_edge():
    graph = KnowledgeGraph()
    # victim_V 는 entitled, attacker_A 는 observed 로만 접근
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="account:222", source="self_created")
    )
    graph.add_observed("attacker_A", "account:222", "read")

    assert graph.entitlement_unknown("account:222") is False  # victim_V 의 entitled_own 이 있으므로
    assert graph.entitlement_unknown("account:999") is True  # 아무도 entitled 없음


def test_apply_observed_is_alias_for_add_observed():
    graph = KnowledgeGraph()
    graph.apply_observed("attacker_A", "account:333", "read", evidence_ref="ev_abc")
    assert len(graph.observed_edges()) == 1
    assert graph.observed_edges()[0]["evidence_ref"] == "ev_abc"
    assert len(graph.entitled_edges()) == 0
