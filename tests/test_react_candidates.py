"""planning/candidates.py 의 resolve_skeleton/graph_menu 단위 테스트.

네트워크 없이, 최소 TargetConfig 와 가짜 그래프만으로 검증한다.
"""
from __future__ import annotations

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Principal
from finredteam.models.knowledge import Entitlement
from finredteam.planning.candidates import (
    CandidateSkeleton,
    graph_menu,
    resolve_skeleton,
    supported_operations,
)
from finredteam.targets.loader import TargetConfig

_RAW_TARGET = {
    "adapter": {
        "name": "test",
        "base_url": "http://localhost:5000",
        "default_currency": {"code": "USD", "minor_unit": 2},
        "auth": {
            "register": {"method": "POST", "path": "/register"},
            "login": {"method": "POST", "path": "/login"},
            "token_header": "Authorization: Bearer {token}",
        },
        "observation": {
            "balance": {
                "method": "GET",
                "path": "/check_balance/{account_number}",
                "auth_required": False,
                "value_path": "$.balance",
            }
        },
        "baseline_ops": [
            {
                "op": "transfer",
                "method": "POST",
                "path": "/transfer",
                "normal_params": {"amount": 10, "to_account": "X"},
            }
        ],
    },
    "settings": {"principals": ["attacker_A", "victim_V"], "initial_balance": "1000.00"},
}


def _target_cfg() -> TargetConfig:
    return TargetConfig.model_validate(_RAW_TARGET)


def _principals() -> list[Principal]:
    return [
        Principal(id="attacker_A", username="a", password="p", account_number="111"),
        Principal(id="victim_V", username="v", password="p", account_number="222"),
    ]


def _graph_with_both_owning_accounts() -> KnowledgeGraph:
    graph = KnowledgeGraph()
    graph.add_entitlement(
        Entitlement(principal="attacker_A", relation="entitled_own", obj="account:111", source="self_created")
    )
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="account:222", source="self_created")
    )
    return graph


def test_resolve_read_balance_builds_skeleton_for_known_account():
    result = resolve_skeleton(
        "read_balance", "attacker_A", "account:222", _principals(), _graph_with_both_owning_accounts(), _target_cfg()
    )
    assert isinstance(result, CandidateSkeleton)
    assert result.operation == "read_balance"
    assert result.subject_account == "222"
    assert result.method == "GET"
    assert result.path == "/check_balance/222"
    assert result.needs_baseline is False


def test_resolve_read_balance_rejects_unknown_account():
    result = resolve_skeleton(
        "read_balance", "attacker_A", "account:999999", _principals(), _graph_with_both_owning_accounts(), _target_cfg()
    )
    assert isinstance(result, str)
    assert "모르는 계좌" in result


def test_resolve_read_balance_rejects_bad_target_format():
    result = resolve_skeleton(
        "read_balance", "attacker_A", "222", _principals(), _graph_with_both_owning_accounts(), _target_cfg()
    )
    assert isinstance(result, str)
    assert "형식" in result


def test_resolve_read_balance_rejects_unknown_principal():
    result = resolve_skeleton(
        "read_balance",
        "no_such_principal",
        "account:222",
        _principals(),
        _graph_with_both_owning_accounts(),
        _target_cfg(),
    )
    assert isinstance(result, str)


def test_resolve_transfer_builds_skeleton_with_counterparty_hint():
    result = resolve_skeleton(
        "transfer", "attacker_A", "account:222", _principals(), _graph_with_both_owning_accounts(), _target_cfg()
    )
    assert isinstance(result, CandidateSkeleton)
    assert result.operation == "transfer"
    assert result.subject_account == "111"
    assert result.hint == {"op": "transfer", "counterparty_account": "222"}
    assert result.needs_baseline is True
    assert set(result.watch_accounts) == {"111", "222"}


def test_resolve_transfer_rejects_sender_without_own_entitlement():
    empty_graph = KnowledgeGraph()  # 아무 entitlement 도 없음
    result = resolve_skeleton("transfer", "attacker_A", "account:222", _principals(), empty_graph, _target_cfg())
    assert isinstance(result, str)
    assert "entitled_own" in result


def test_resolve_skeleton_rejects_unsupported_operation():
    result = resolve_skeleton(
        "delete_all_accounts",
        "attacker_A",
        "account:222",
        _principals(),
        _graph_with_both_owning_accounts(),
        _target_cfg(),
    )
    assert isinstance(result, str)
    assert "delete_all_accounts" in result


def test_graph_menu_lists_known_accounts_and_entitlements():
    menu = graph_menu(_principals(), _graph_with_both_owning_accounts())
    assert set(menu["principals"]) == {"attacker_A", "victim_V"}
    assert set(menu["known_accounts"]) == {"account:111", "account:222"}
    assert menu["entitled_own"] == {"attacker_A": "account:111", "victim_V": "account:222"}
    assert menu["supported_operations"] == supported_operations()
